"""Collector entrypoint: run the WebSocket feed plus a periodic REST audit.

    python -m kmm.collect --config config/markets.yaml

Three things run concurrently:

  1. The WebSocket feed, which is the real dataset: every book change with a
     sequence number and an exchange timestamp.
  2. A slow REST poll of the full order book, which exists to CHECK the
     WebSocket-reconstructed book rather than to add data. If our reconstruction
     drifts from what REST reports, something is wrong with the delta handling
     and we want to know before running a scanner over the results.
  3. A slower metadata refresh, which picks up status changes and, once a market
     resolves, the outcome that Phase 3 needs.

Stop with Ctrl-C. The run is recorded in collector_runs with a start and end
time, so a later analysis can tell the difference between "no mispricings
happened" and "the collector was not running".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
from pathlib import Path

from . import __version__
from .book import OrderBook
from .config import CollectorConfig, load_credentials
from .auth import KalshiSigner
from .prices import parse_levels
from .ratelimit import read_bucket
from .rest import KalshiREST, KalshiRESTError
from .storage import Store, now_ms
from .ws import WSCollector


async def rest_audit_loop(rest: KalshiREST, store: Store, ws: WSCollector, tickers: list[str],
                          interval: float, stop: asyncio.Event) -> None:
    """Poll the full book over REST and compare it to the live reconstruction."""
    while not stop.is_set():
        for ticker in tickers:
            if stop.is_set():
                break
            try:
                raw = await rest.get_orderbook(ticker)
            except KalshiRESTError as exc:
                store.write_gap("rest_error", ticker=ticker, detail=str(exc))
                continue

            snapshot = OrderBook(ticker)
            snapshot.apply_snapshot(
                parse_levels(raw.get("yes_dollars") or raw.get("yes")),
                parse_levels(raw.get("no_dollars") or raw.get("no")),
                seq=None,
                ts_ms=None,
            )
            store.write_book(snapshot.to_row("rest", now_ms(), None))

            live = ws.books.get(ticker)
            if live is not None and live.ready:
                a, b = live.best_yes_bid, snapshot.best_yes_bid
                c, d = live.best_no_bid, snapshot.best_no_bid
                # Compare top of book only. Deeper levels legitimately differ by
                # the handful of milliseconds between the two reads.
                if (a[0] if a else None) != (b[0] if b else None) or (c[0] if c else None) != (d[0] if d else None):
                    store.write_gap(
                        "rest_error", ticker=ticker,
                        detail=f"ws/rest top-of-book mismatch ws_yes={a} rest_yes={b} ws_no={c} rest_no={d}",
                    )
        with_timeout = asyncio.wait_for(stop.wait(), timeout=interval)
        try:
            await with_timeout
        except asyncio.TimeoutError:
            pass


async def metadata_loop(rest: KalshiREST, store: Store, tickers: list[str], stop: asyncio.Event,
                        interval: float = 900.0) -> None:
    """Refresh market metadata, including `result` once a market settles."""
    while not stop.is_set():
        for ticker in tickers:
            try:
                store.write_market_meta(ticker, await rest.get_market(ticker))
            except KalshiRESTError as exc:
                store.write_gap("rest_error", ticker=ticker, detail=f"meta: {exc}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def status_loop(ws: WSCollector, stop: asyncio.Event, interval: float = 60.0) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            print(f"[status] {json.dumps(ws.stats)}")


async def main_async(args: argparse.Namespace) -> None:
    cfg = CollectorConfig.from_yaml(args.config)
    creds = load_credentials(args.secrets) if args.secrets else load_credentials()
    signer = KalshiSigner.from_file(creds.key_id, creds.private_key_path)

    store = Store(cfg.db_path)
    store.start_run(
        env=creds.env, transport="websocket+rest_audit", tickers=cfg.tickers,
        version=__version__, notes=args.notes or "",
    )
    print(f"[init] env={creds.env} db={cfg.db_path} tickers={len(cfg.tickers)} run_id={store.run_id}")

    rest = KalshiREST(signer, env=creds.env, bucket=read_bucket(cfg.tier))
    try:
        status = await rest.get_exchange_status()
        print(f"[init] exchange status: {status}")
    except KalshiRESTError as exc:
        print(f"[init] could not reach the exchange: {exc}")
        store.close()
        return

    ws = WSCollector(signer, store, cfg.tickers, env=creds.env,
                     raw_dir=cfg.raw_dir, record_raw=cfg.record_raw)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows does not support add_signal_handler for SIGTERM; the
            # KeyboardInterrupt path in main() covers Ctrl-C there.
            pass

    tasks = [
        asyncio.create_task(ws.run(stop), name="ws"),
        asyncio.create_task(rest_audit_loop(rest, store, ws, cfg.tickers, cfg.rest_poll_seconds, stop), name="audit"),
        asyncio.create_task(metadata_loop(rest, store, cfg.tickers, stop), name="meta"),
        asyncio.create_task(status_loop(ws, stop), name="status"),
    ]
    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        stop.set()
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await rest.aclose()
        print(f"[done] {json.dumps(ws.stats)}")
        store.end_run()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Kalshi order book and trade data. Read only.")
    parser.add_argument("--config", default="config/markets.yaml", type=Path)
    parser.add_argument("--secrets", default=None, type=Path)
    parser.add_argument("--notes", default="", help="free text stored with the run record")
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[done] interrupted")


if __name__ == "__main__":
    main()
