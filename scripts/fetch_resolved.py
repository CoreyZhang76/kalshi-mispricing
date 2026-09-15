"""Build the calibration dataset from markets that have already resolved.

    .venv/Scripts/python.exe scripts/fetch_resolved.py --series KXHIGHNY --days 90

Calibration does not need the live collector. It needs price histories for
markets whose outcome is now known, and Kalshi serves both: `/markets` with
status=settled carries the `result` field, and the candlestick endpoint carries
the whole quoted price path. So this analysis can produce a genuine result from
historical data on day one, instead of waiting weeks for live collection.

Two decisions about what counts as an observation.

**The mid of bid and ask, not the last trade price.** A last trade can be hours
stale on a quiet market, and it reflects one participant crossing rather than the
market's standing view. The mid of the resting quotes is what the book actually
believed at that moment. Where a side is missing there is no mid, and the
observation is dropped rather than filled in.

**Observations are thinned to one per market per hour.** Sampling every minute
would multiply rows without adding information, since one market still has only
one outcome, and it would make a small dataset look large. See
`calibration.effective_sample_note` for why the row count is the least meaningful
of the three sample sizes.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kmm.auth import KalshiSigner
from kmm.calibration import Observation, summarise
from kmm.config import load_credentials
from kmm.rest import KalshiREST, KalshiRESTError

SCHEMA = """
CREATE TABLE IF NOT EXISTS calibration_obs (
    ticker        TEXT NOT NULL,
    event_ticker  TEXT,
    series_ticker TEXT,
    ts_ms         INTEGER NOT NULL,
    yes_bid_c     INTEGER,
    yes_ask_c     INTEGER,
    implied_prob  REAL NOT NULL,
    outcome       INTEGER NOT NULL,
    hours_to_res  REAL NOT NULL,
    fetched_ms    INTEGER NOT NULL,
    PRIMARY KEY (ticker, ts_ms)
);
CREATE INDEX IF NOT EXISTS ix_calib_series ON calibration_obs(series_ticker);
"""


def _cents(value) -> int | None:
    if value in (None, ""):
        return None
    return int(round(float(value) * 100))


def candles_to_observations(market: dict, candles: list[dict]) -> list[Observation]:
    """Turn one resolved market's candlestick history into observations."""
    result = (market.get("result") or "").lower()
    if result not in ("yes", "no"):
        return []  # voided, or not actually settled: no ground truth to compare against
    outcome = 1 if result == "yes" else 0

    close_ts = market.get("close_time")
    if isinstance(close_ts, str):
        from datetime import datetime
        close_ts = int(datetime.fromisoformat(close_ts.replace("Z", "+00:00")).timestamp())
    if not close_ts:
        return []

    out: list[Observation] = []
    for candle in candles:
        end_ts = candle.get("end_period_ts")
        bid = _cents((candle.get("yes_bid") or {}).get("close_dollars"))
        ask = _cents((candle.get("yes_ask") or {}).get("close_dollars"))
        if end_ts is None or bid is None or ask is None:
            continue  # one-sided or empty book: no mid exists, so no observation
        if not (0 < bid <= ask < 100):
            continue  # nonsensical or locked quote
        out.append(
            Observation(
                ticker=market["ticker"],
                event_ticker=market.get("event_ticker") or market["ticker"],
                ts_ms=int(end_ts) * 1000,
                implied_prob=(bid + ask) / 200.0,
                outcome=outcome,
                hours_to_resolution=max(0.0, (close_ts - int(end_ts)) / 3600.0),
            )
        )
    return out


async def fetch(series_ticker: str, days: int, db_path: Path, limit_markets: int) -> list[Observation]:
    creds = load_credentials()
    signer = KalshiSigner.from_file(creds.key_id, creds.private_key_path)
    now = int(time.time())
    window_start = now - days * 86400

    collected: list[Observation] = []
    async with KalshiREST(signer, env=creds.env) as rest:
        markets = await rest.get_markets(
            series_ticker=series_ticker, status="settled", min_settled_ts=window_start
        )
        print(f"{len(markets)} settled market(s) in {series_ticker} over the last {days} days")
        if limit_markets:
            markets = markets[:limit_markets]

        for i, market in enumerate(markets, 1):
            open_ts = market.get("open_time")
            if isinstance(open_ts, str):
                from datetime import datetime
                open_ts = int(datetime.fromisoformat(open_ts.replace("Z", "+00:00")).timestamp())
            close_ts = market.get("close_time")
            if isinstance(close_ts, str):
                from datetime import datetime
                close_ts = int(datetime.fromisoformat(close_ts.replace("Z", "+00:00")).timestamp())
            if not open_ts or not close_ts:
                continue
            try:
                candles = await rest.get_candlesticks(
                    series_ticker=market.get("series_ticker") or series_ticker,
                    ticker=market["ticker"],
                    start_ts=open_ts, end_ts=close_ts,
                    period_interval=60,  # hourly: one observation per market per hour
                )
            except KalshiRESTError as exc:
                print(f"  [{i}/{len(markets)}] {market['ticker']}: {exc}")
                continue
            obs = candles_to_observations(market, candles)
            collected.extend(obs)
            print(f"  [{i}/{len(markets)}] {market['ticker']}: {len(obs)} observation(s), "
                  f"resolved {market.get('result')}")

    write(db_path, collected, series_ticker)
    return collected


def write(db_path: Path, obs: list[Observation], series_ticker: str) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        now_ms = int(time.time() * 1000)
        conn.executemany(
            "INSERT OR REPLACE INTO calibration_obs (ticker, event_ticker, series_ticker, ts_ms, "
            "yes_bid_c, yes_ask_c, implied_prob, outcome, hours_to_res, fetched_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(o.ticker, o.event_ticker, series_ticker, o.ts_ms, None, None,
              o.implied_prob, o.outcome, o.hours_to_resolution, now_ms) for o in obs],
        )
        conn.commit()
    finally:
        conn.close()


def load(db_path: Path, series_ticker: str | None = None) -> list[Observation]:
    """Read observations back out, for analysis without refetching."""
    conn = sqlite3.connect(db_path)
    try:
        sql = ("SELECT ticker, event_ticker, ts_ms, implied_prob, outcome, hours_to_res "
               "FROM calibration_obs")
        params: tuple = ()
        if series_ticker:
            sql += " WHERE series_ticker = ?"
            params = (series_ticker,)
        return [Observation(*row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch resolved markets for calibration analysis.")
    parser.add_argument("--series", required=True, help="series ticker, e.g. KXHIGHNY")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--db", default="data/calibration.sqlite", type=Path)
    parser.add_argument("--limit", type=int, default=0, help="cap markets fetched, for a quick look")
    args = parser.parse_args()

    obs = asyncio.run(fetch(args.series, args.days, args.db, args.limit))
    print(f"\nstored {len(obs)} observation(s) in {args.db}\n")
    print(summarise(obs))


if __name__ == "__main__":
    main()
