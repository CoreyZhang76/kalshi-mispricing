"""SQLite storage for collected market data.

Why SQLite rather than parquet for the raw feed: the collector appends
continuously and we want to query partial data while it is still running.
Parquet is a write-once columnar format and does not like either of those. The
analysis stages read from SQLite and export parquet when a frozen snapshot is
wanted.

Two timestamps are recorded on every row and they mean different things:

    ts_ms    the exchange's timestamp, when Kalshi says the event happened
    recv_ms  our clock, when the bytes arrived here

They differ by network latency plus clock skew. Any claim about how fast a
mispricing closed has to be made against ts_ms, because recv_ms includes our own
delay and would make the market look slower than it is. Keeping both lets us
measure that delay instead of assuming it away.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- One row per collector process, so any data point can be traced back to the
-- run that produced it and to the code version that was running.
CREATE TABLE IF NOT EXISTS collector_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ms  INTEGER NOT NULL,
    ended_ms    INTEGER,
    env         TEXT NOT NULL,
    transport   TEXT NOT NULL,      -- 'websocket' or 'rest_poll'
    tickers     TEXT NOT NULL,      -- json array
    version     TEXT NOT NULL,
    notes       TEXT
);

-- Market metadata and, once resolved, the outcome. Phase 3 calibration reads
-- `result` from here. Re-fetched periodically, so we keep a history rather than
-- overwriting: status and close time can change while a market is live.
CREATE TABLE IF NOT EXISTS market_meta (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker         TEXT NOT NULL,
    fetched_ms     INTEGER NOT NULL,
    event_ticker   TEXT,
    series_ticker  TEXT,
    title          TEXT,
    subtitle       TEXT,
    status         TEXT,
    result         TEXT,            -- 'yes', 'no', '' while unresolved
    open_ts        INTEGER,
    close_ts       INTEGER,
    expiration_ts  INTEGER,
    raw_json       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_meta_ticker ON market_meta(ticker, fetched_ms);

-- Top of book plus the full ladder. One row per book change (websocket) or per
-- poll (REST). `levels_json` holds both sides so depth-aware sizing is possible
-- later without re-collecting.
CREATE TABLE IF NOT EXISTS book (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL,
    ticker       TEXT NOT NULL,
    ts_ms        INTEGER,           -- exchange time, null if Kalshi did not send one
    recv_ms      INTEGER NOT NULL,  -- our clock
    seq          INTEGER,           -- websocket sequence number, null for REST
    source       TEXT NOT NULL,     -- 'ws_snapshot' | 'ws_delta' | 'rest'
    yes_bid_c    INTEGER,           -- best YES bid, whole cents
    yes_bid_sz   REAL,
    no_bid_c     INTEGER,           -- best NO bid, whole cents
    no_bid_sz    REAL,
    yes_ask_c    INTEGER,           -- derived: 100 - no_bid_c
    yes_ask_sz   REAL,              -- same size as the NO bid that implies it
    levels_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_book_ticker_ts ON book(ticker, recv_ms);

CREATE TABLE IF NOT EXISTS trades (
    trade_id     TEXT PRIMARY KEY,
    run_id       INTEGER NOT NULL,
    ticker       TEXT NOT NULL,
    ts_ms        INTEGER,
    recv_ms      INTEGER NOT NULL,
    yes_price_c  INTEGER,
    no_price_c   INTEGER,
    count        REAL,
    taker_side   TEXT
);
CREATE INDEX IF NOT EXISTS ix_trades_ticker_ts ON trades(ticker, ts_ms);

-- Data-quality log. A websocket sequence gap means we missed book updates and
-- our reconstructed book was wrong until the next snapshot. Any analysis window
-- overlapping a gap has to be excluded or flagged, so the gaps are recorded as
-- first-class data rather than logged to stderr and forgotten.
CREATE TABLE IF NOT EXISTS feed_gaps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL,
    ticker      TEXT,
    detected_ms INTEGER NOT NULL,
    expected_seq INTEGER,
    got_seq     INTEGER,
    kind        TEXT NOT NULL,      -- 'seq_gap' | 'disconnect' | 'rest_error'
    detail      TEXT
);
"""


class Store:
    """Thin wrapper over a SQLite connection with batched commits."""

    def __init__(self, path: str | Path, commit_every: int = 200):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so an asyncio task set can share one connection.
        # All writes funnel through this object, so there is no concurrent write.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.commit_every = commit_every
        self._pending = 0
        self.run_id: int | None = None

    def start_run(self, env: str, transport: str, tickers: list[str], version: str, notes: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO collector_runs (started_ms, env, transport, tickers, version, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now_ms(), env, transport, json.dumps(tickers), version, notes),
        )
        self.conn.commit()
        self.run_id = int(cur.lastrowid)
        return self.run_id

    def end_run(self) -> None:
        if self.run_id is not None:
            self.conn.execute(
                "UPDATE collector_runs SET ended_ms = ? WHERE run_id = ?", (now_ms(), self.run_id)
            )
        self.conn.commit()

    def _tick(self) -> None:
        self._pending += 1
        if self._pending >= self.commit_every:
            self.conn.commit()
            self._pending = 0

    def write_market_meta(self, ticker: str, raw: dict) -> None:
        self.conn.execute(
            "INSERT INTO market_meta (ticker, fetched_ms, event_ticker, series_ticker, title, "
            "subtitle, status, result, open_ts, close_ts, expiration_ts, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticker,
                now_ms(),
                raw.get("event_ticker"),
                raw.get("series_ticker"),
                raw.get("title"),
                raw.get("subtitle") or raw.get("yes_sub_title"),
                raw.get("status"),
                raw.get("result", ""),
                _iso_or_epoch(raw.get("open_time")),
                _iso_or_epoch(raw.get("close_time")),
                _iso_or_epoch(raw.get("expiration_time")),
                json.dumps(raw, separators=(",", ":")),
            ),
        )
        self._tick()

    def write_book(self, row: dict) -> None:
        self.conn.execute(
            "INSERT INTO book (run_id, ticker, ts_ms, recv_ms, seq, source, yes_bid_c, yes_bid_sz, "
            "no_bid_c, no_bid_sz, yes_ask_c, yes_ask_sz, levels_json) "
            "VALUES (:run_id, :ticker, :ts_ms, :recv_ms, :seq, :source, :yes_bid_c, :yes_bid_sz, "
            ":no_bid_c, :no_bid_sz, :yes_ask_c, :yes_ask_sz, :levels_json)",
            {**row, "run_id": self.run_id},
        )
        self._tick()

    def write_trade(self, row: dict) -> None:
        # A websocket reconnect replays recent trades, so collisions are normal.
        self.conn.execute(
            "INSERT OR IGNORE INTO trades (trade_id, run_id, ticker, ts_ms, recv_ms, yes_price_c, "
            "no_price_c, count, taker_side) "
            "VALUES (:trade_id, :run_id, :ticker, :ts_ms, :recv_ms, :yes_price_c, :no_price_c, "
            ":count, :taker_side)",
            {**row, "run_id": self.run_id},
        )
        self._tick()

    def write_gap(self, kind: str, ticker: str | None = None, expected_seq: int | None = None,
                  got_seq: int | None = None, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO feed_gaps (run_id, ticker, detected_ms, expected_seq, got_seq, kind, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.run_id, ticker, now_ms(), expected_seq, got_seq, kind, detail),
        )
        self.conn.commit()  # gaps are rare and important, never buffer them

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


def now_ms() -> int:
    return int(time.time() * 1000)


def _iso_or_epoch(value) -> int | None:
    """Kalshi returns RFC3339 strings for times. Store epoch seconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    from datetime import datetime

    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


@contextmanager
def open_store(path: str | Path) -> Iterator[Store]:
    store = Store(path)
    try:
        yield store
    finally:
        store.end_run()
        store.close()
