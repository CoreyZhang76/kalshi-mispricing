"""Offline self-test. No network, no credentials, no Kalshi account needed.

    .venv/Scripts/python.exe scripts/selftest.py

Covers the parts that are easy to get quietly wrong: the YES/NO price identity,
delta application, sequence-gap detection, the signing string, and the token
bucket's pacing.
"""

from __future__ import annotations

import asyncio
import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kmm.auth import KalshiSigner
from kmm.book import OrderBook, SequenceTracker
from kmm.prices import dollars_to_cents, implied_probability, parse_levels
from kmm.ratelimit import TokenBucket, read_bucket
from kmm.storage import Store


def test_prices() -> None:
    assert dollars_to_cents("0.3700") == 37
    assert dollars_to_cents("0.08") == 8
    assert dollars_to_cents(0.995) == 100  # rounds to the nearest tick
    assert parse_levels([["0.2200", "333.00"], ["0.0800", "300.00"]]) == [(8, 300.0), (22, 333.0)]
    assert parse_levels(None) == []
    assert implied_probability(40, 44) == 0.42
    print("prices ok")


def test_book() -> None:
    book = OrderBook("TEST")
    # YES bids at 8c and 22c; NO bids at 54c and 56c.
    book.apply_snapshot(
        parse_levels([["0.0800", "300.00"], ["0.2200", "333.00"]]),
        parse_levels([["0.5400", "20.00"], ["0.5600", "146.00"]]),
        seq=1,
    )
    assert book.best_yes_bid == (22, 333.0)
    assert book.best_no_bid == (56, 146.0)
    # The best NO bid at 56c is a YES offer at 44c, same size.
    assert book.best_yes_ask == (44, 146.0)
    assert book.spread_c == 22
    assert not book.is_crossed()

    # Someone lifts 100 of the 56c NO bid: size falls, price level survives.
    book.apply_delta("no", 56, -100.0, seq=2)
    assert book.best_no_bid == (56, 46.0)
    # The rest of that level is cancelled: the level disappears and the YES ask
    # widens to the next NO bid down, 54c -> YES offer at 46c.
    book.apply_delta("no", 56, -46.0, seq=3)
    assert book.best_no_bid == (54, 20.0)
    assert book.best_yes_ask == (46, 20.0)

    # New YES bid appears at a level that did not exist.
    book.apply_delta("yes", 30, 12.0, seq=4)
    assert book.best_yes_bid == (30, 12.0)
    assert book.spread_c == 16

    row = book.to_row("ws_delta", recv_ms=123, seq=4)
    assert row["yes_bid_c"] == 30 and row["yes_ask_c"] == 46 and row["no_bid_c"] == 54
    print("book ok")


def test_sequence() -> None:
    tracker = SequenceTracker()
    assert tracker.check(1, 5)[0] is True     # first message on a sid sets the baseline
    assert tracker.check(1, 6)[0] is True
    ok, expected = tracker.check(1, 9)        # 7 and 8 never arrived
    assert ok is False and expected == 7
    assert tracker.check(1, 10)[0] is True    # resynced to the new position
    # A second subscription is tracked independently.
    assert tracker.check(2, 1)[0] is True
    print("sequence ok")


def test_signing() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = KalshiSigner(key_id="test-key-id", private_key=key)

    before = int(time.time() * 1000)
    headers = signer.headers("get", "/trade-api/v2/markets")
    after = int(time.time() * 1000)

    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    ts = int(headers["KALSHI-ACCESS-TIMESTAMP"])
    assert before <= ts <= after, "timestamp must be milliseconds from the live clock"
    assert len(headers["KALSHI-ACCESS-TIMESTAMP"]) == 13, "13 digits means ms, 10 would be seconds"

    # Verify the signature the way Kalshi's server would: method upper-cased,
    # concatenated with no separators, RSA-PSS/SHA-256 with digest-length salt.
    message = headers["KALSHI-ACCESS-TIMESTAMP"] + "GET" + "/trade-api/v2/markets"
    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        message.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256.digest_size),
        hashes.SHA256(),
    )

    # Query strings must be rejected rather than silently signed.
    try:
        signer.headers("GET", "/trade-api/v2/markets?limit=5")
    except ValueError:
        pass
    else:
        raise AssertionError("signing a path with a query string should raise")
    print("signing ok")


async def test_bucket() -> None:
    # 10 tokens/sec, capacity 10. Starts full, so the first 10 are free and the
    # eleventh has to wait a full second.
    bucket = TokenBucket(rate=10.0, capacity=10.0)
    start = time.monotonic()
    for _ in range(10):
        await bucket.acquire(cost=1.0)
    assert time.monotonic() - start < 0.05, "a full bucket should not throttle"
    await bucket.acquire(cost=1.0)
    elapsed = time.monotonic() - start
    assert 0.08 < elapsed < 0.4, f"expected ~0.1s wait for one token, got {elapsed:.3f}s"

    basic = read_bucket("basic")
    assert basic.rate == 160.0 and basic.capacity == 160.0  # 200 * 0.8 safety, 1s of burst
    advanced = read_bucket("advanced")
    assert advanced.capacity == advanced.rate * 2  # higher tiers hold 2s of burst
    print("ratelimit ok")


def test_storage(tmp: Path) -> None:
    store = Store(tmp / "selftest.sqlite")
    run_id = store.start_run("demo", "websocket", ["A", "B"], "test")
    assert run_id >= 1

    book = OrderBook("A")
    book.apply_snapshot([(20, 5.0)], [(70, 8.0)], seq=1)
    store.write_book(book.to_row("ws_snapshot", 1000, 1))

    store.write_trade({"trade_id": "t1", "ticker": "A", "ts_ms": 1000, "recv_ms": 1001,
                       "yes_price_c": 25, "no_price_c": 75, "count": 10.0, "taker_side": "yes"})
    store.write_trade({"trade_id": "t1", "ticker": "A", "ts_ms": 1000, "recv_ms": 1002,
                       "yes_price_c": 25, "no_price_c": 75, "count": 10.0, "taker_side": "yes"})
    store.write_gap("seq_gap", ticker="A", expected_seq=2, got_seq=5)
    store.write_market_meta("A", {"title": "test", "status": "settled", "result": "yes",
                                  "close_time": "2026-05-01T12:00:00Z"})
    store.end_run()

    conn = store.conn
    assert conn.execute("SELECT COUNT(*) FROM book").fetchone()[0] == 1
    # The duplicate trade_id must not produce a second row: reconnects replay trades.
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM feed_gaps").fetchone()[0] == 1
    row = conn.execute("SELECT result, close_ts FROM market_meta").fetchone()
    assert row[0] == "yes" and row[1] == 1777636800  # 2026-05-01T12:00:00Z
    assert conn.execute("SELECT ended_ms FROM collector_runs").fetchone()[0] is not None
    # yes_ask is derived from the no bid, not stored by the feed.
    assert conn.execute("SELECT yes_ask_c FROM book").fetchone()[0] == 30
    store.close()
    print("storage ok")


def main() -> int:
    tmp = Path(__file__).resolve().parents[1] / "data" / "_selftest"
    tmp.mkdir(parents=True, exist_ok=True)
    for stale in tmp.glob("selftest.sqlite*"):
        stale.unlink()

    test_prices()
    test_book()
    test_sequence()
    test_signing()
    asyncio.run(test_bucket())
    test_storage(tmp)
    print("\nall offline tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
