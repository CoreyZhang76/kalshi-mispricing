"""WebSocket collector: subscribes to orderbook_delta and trade, writes to SQLite.

Protocol summary (docs.kalshi.com asyncapi.yaml):

  - The connection itself requires auth. The same three KALSHI-ACCESS-* headers
    go on the HTTP upgrade request, signing the path "/trade-api/ws/v2" with
    method GET. Some channels are public data, but the socket is not.
  - Subscribe by sending
        {"id": 1, "cmd": "subscribe",
         "params": {"channels": ["orderbook_delta"], "market_tickers": [...]}}
    The server replies {"type": "subscribed", "msg": {"channel": ..., "sid": N}}.
  - Data messages look like
        {"type": "orderbook_snapshot"|"orderbook_delta"|"trade",
         "sid": N, "seq": K, "msg": {...}}
  - Kalshi sends a WebSocket Ping frame every 10 seconds with body "heartbeat".
    The `websockets` library answers Pongs automatically, and its own ping_interval
    gives us a dead-connection detector from this side.

Reconnect policy: exponential backoff, then resubscribe from scratch. Because a
fresh subscription always begins with a snapshot, a reconnect self-heals the
book. What it cannot heal is the hole in the record while we were away, so the
disconnect is written to feed_gaps.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import random
from pathlib import Path

import websockets

from . import endpoints
from .auth import KalshiSigner
from .book import OrderBook, SequenceTracker
from .prices import dollars_to_cents, parse_levels, parse_size
from .storage import Store, now_ms


class WSCollector:
    def __init__(
        self,
        signer: KalshiSigner,
        store: Store,
        tickers: list[str],
        env: str = "prod",
        raw_dir: Path | None = None,
        record_raw: bool = True,
    ):
        self.signer = signer
        self.store = store
        self.tickers = tickers
        self.env = env
        self.url = endpoints.ws_url(env)
        self.books: dict[str, OrderBook] = {t: OrderBook(t) for t in tickers}
        self.seq = SequenceTracker()
        self.raw_path: Path | None = None
        if record_raw and raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
            self.raw_path = raw_dir / f"ws_{now_ms()}.jsonl.gz"
        self._raw_fh = None
        self._msg_id = 0
        self.stats = {"messages": 0, "snapshots": 0, "deltas": 0, "trades": 0, "gaps": 0, "reconnects": 0}

    # ---- raw capture -----------------------------------------------------
    # Every byte the exchange sends is also written verbatim, compressed. Parsing
    # is where bugs live, and a raw log means a parser fix can be replayed over
    # old data instead of the data being lost.

    def _open_raw(self):
        if self.raw_path is not None and self._raw_fh is None:
            self._raw_fh = gzip.open(self.raw_path, "at", encoding="utf-8")

    def _write_raw(self, payload: str) -> None:
        if self._raw_fh is not None:
            self._raw_fh.write(json.dumps({"recv_ms": now_ms(), "raw": payload}) + "\n")

    def _close_raw(self) -> None:
        if self._raw_fh is not None:
            self._raw_fh.close()
            self._raw_fh = None

    # ---- main loop -------------------------------------------------------

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        self._open_raw()
        backoff = 1.0
        try:
            while not stop.is_set():
                try:
                    await self._session(stop)
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - reconnect on anything
                    self.stats["reconnects"] += 1
                    self.store.write_gap("disconnect", detail=f"{type(exc).__name__}: {exc}")
                    # Jittered backoff. Without jitter, a client that reconnects
                    # on a fixed schedule after an exchange-side blip lands in
                    # lockstep with every other client doing the same thing.
                    wait = min(backoff, 60.0) * (0.5 + random.random())
                    print(f"[ws] disconnected: {exc}. reconnecting in {wait:.1f}s")
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=wait)
                    backoff = min(backoff * 2, 60.0)
        finally:
            self._close_raw()

    async def _session(self, stop: asyncio.Event) -> None:
        # The signed path for the socket handshake is the WS path, not a REST one.
        headers = self.signer.headers("GET", endpoints.WS_PATH)
        # Only the three auth headers belong on the upgrade request.
        headers = {k: v for k, v in headers.items() if k.startswith("KALSHI-")}

        async with websockets.connect(
            self.url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
            max_queue=4096,
            open_timeout=20,
        ) as sock:
            print(f"[ws] connected to {self.url}")
            # A fresh subscription always starts with a snapshot, so any book
            # state we were holding is stale by definition. Drop it.
            self.seq.reset()
            for book in self.books.values():
                book.ready = False

            await self._send(sock, "subscribe", {"channels": ["orderbook_delta"], "market_tickers": self.tickers})
            await self._send(sock, "subscribe", {"channels": ["trade"], "market_tickers": self.tickers})

            while not stop.is_set():
                try:
                    payload = await asyncio.wait_for(sock.recv(), timeout=90)
                except asyncio.TimeoutError:
                    # 90s of total silence on a feed that heartbeats every 10s
                    # means the connection is dead even if TCP has not noticed.
                    raise ConnectionError("no data for 90s")
                self._write_raw(payload)
                self.stats["messages"] += 1
                self._handle(json.loads(payload))

    async def _send(self, sock, cmd: str, params: dict) -> None:
        self._msg_id += 1
        await sock.send(json.dumps({"id": self._msg_id, "cmd": cmd, "params": params}))

    # ---- message handling ------------------------------------------------

    def _handle(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "subscribed":
            sid = message.get("msg", {}).get("sid")
            channel = message.get("msg", {}).get("channel")
            print(f"[ws] subscribed channel={channel} sid={sid}")
            return
        if kind == "error":
            detail = json.dumps(message.get("msg", {}))
            print(f"[ws] server error: {detail}")
            self.store.write_gap("disconnect", detail=f"server error: {detail}")
            return
        if kind == "orderbook_snapshot":
            self._on_snapshot(message)
        elif kind == "orderbook_delta":
            self._on_delta(message)
        elif kind == "trade":
            self._on_trade(message)

    def _seq_ok(self, message: dict, ticker: str | None) -> bool:
        sid, seq = message.get("sid"), message.get("seq")
        if sid is None:
            return True
        ok, expected = self.seq.check(sid, seq)
        if not ok:
            self.stats["gaps"] += 1
            self.store.write_gap("seq_gap", ticker=ticker, expected_seq=expected, got_seq=seq,
                                 detail=f"sid={sid}")
            print(f"[ws] SEQUENCE GAP sid={sid} expected={expected} got={seq}: book is unreliable until resnapshot")
        return ok

    def _on_snapshot(self, message: dict) -> None:
        msg = message["msg"]
        ticker = msg["market_ticker"]
        self._seq_ok(message, ticker)
        book = self.books.setdefault(ticker, OrderBook(ticker))
        book.apply_snapshot(
            parse_levels(msg.get("yes_dollars_fp")),
            parse_levels(msg.get("no_dollars_fp")),
            seq=message.get("seq"),
            ts_ms=msg.get("ts_ms"),
        )
        self.stats["snapshots"] += 1
        self.store.write_book(book.to_row("ws_snapshot", now_ms(), message.get("seq")))

    def _on_delta(self, message: dict) -> None:
        msg = message["msg"]
        ticker = msg["market_ticker"]
        ok = self._seq_ok(message, ticker)
        book = self.books.setdefault(ticker, OrderBook(ticker))
        if not ok or not book.ready:
            # Applying a delta to a book we know is wrong just spreads the error.
            # Mark it unusable and wait for the next snapshot.
            book.ready = False
            return
        book.apply_delta(
            side=msg["side"],
            price_c=dollars_to_cents(msg["price_dollars"]),
            delta=parse_size(msg["delta_fp"]),
            seq=message.get("seq"),
            ts_ms=msg.get("ts_ms"),
        )
        self.stats["deltas"] += 1
        self.store.write_book(book.to_row("ws_delta", now_ms(), message.get("seq")))

    def _on_trade(self, message: dict) -> None:
        msg = message["msg"]
        self.stats["trades"] += 1
        self.store.write_trade(
            {
                "trade_id": msg.get("trade_id"),
                "ticker": msg.get("market_ticker"),
                "ts_ms": msg.get("ts_ms"),
                "recv_ms": now_ms(),
                "yes_price_c": dollars_to_cents(msg["yes_price_dollars"]) if msg.get("yes_price_dollars") else None,
                "no_price_c": dollars_to_cents(msg["no_price_dollars"]) if msg.get("no_price_dollars") else None,
                "count": parse_size(msg["count_fp"]) if msg.get("count_fp") else None,
                "taker_side": msg.get("taker_side"),
            }
        )
