"""Offline self-test. No network, no credentials, no Kalshi account needed.

    .venv/Scripts/python.exe scripts/selftest.py

Covers the parts that are easy to get quietly wrong: the YES/NO price identity,
delta application, sequence-gap detection, the signing string, and the token
bucket's pacing.
"""

from __future__ import annotations

import asyncio
import math
import random
import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kmm.auth import KalshiSigner
from kmm.book import OrderBook, SequenceTracker
from kmm.calibration import (Observation, brier_decomposition, brier_score, by_horizon,
                             expected_reliability_noise,
                             clustered_bootstrap_brier, effective_sample_note,
                             reliability_table, summarise, wilson_interval)
from kmm.fees import FeeModel, net_edge_cents, round_trip_threshold_cents
from kmm.linkage import (Interval, InferredSet, infer_sets, implication_pairs,
                         market_interval, tiles_the_line)
from kmm.prices import dollars_to_cents, implied_probability, parse_levels
from kmm.ratelimit import TokenBucket, read_bucket
from kmm.relations import Quote, check_complement, check_implication, check_ladder, check_partition
from kmm.scanner import Scanner
from kmm.scanner import summarise as scanner_summarise
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


def test_relations() -> None:
    # --- complement: only fires on a crossed book, which should be impossible ---
    assert check_complement(Quote("M", yes_bid_c=40, yes_bid_sz=10, yes_ask_c=44, yes_ask_sz=10)) is None
    crossed = check_complement(Quote("M", yes_bid_c=46, yes_bid_sz=10, yes_ask_c=44, yes_ask_sz=7))
    assert crossed is not None and crossed.edge_c == 2 and crossed.max_size == 7
    # A one-sided book cannot be traded, so no relation is asserted over it.
    assert check_complement(Quote("M", yes_bid_c=40, yes_bid_sz=10)) is None

    # --- implication: "above 3.1" (sub) entails "above 3.0" (sup) ---
    sub = Quote("CPI-T3.1", yes_bid_c=45, yes_bid_sz=20, yes_ask_c=47, yes_ask_sz=20)
    sup = Quote("CPI-T3.0", yes_bid_c=38, yes_bid_sz=30, yes_ask_c=40, yes_ask_sz=12)
    # Selling the narrower event at 45 while buying the wider at 40 is a 5c credit
    # on a position that cannot lose at settlement.
    v = check_implication(sub, sup)
    assert v is not None and v.edge_c == 5 and v.max_size == 12  # thinnest leg caps it
    assert v.gross_cents == 60
    assert [leg.action for leg in v.legs] == ["buy_yes", "sell_yes"]

    # Correctly ordered prices produce nothing, and the spread alone must not
    # trigger it: mids here are 46 and 39, wrong order, yet no arbitrage exists.
    ok_sup = Quote("CPI-T3.0", yes_bid_c=44, yes_bid_sz=30, yes_ask_c=46, yes_ask_sz=30)
    assert check_implication(sub, ok_sup) is None
    # Direction matters: the relation is one-way, so swapping the arguments
    # asserts something that is not true and must not fire here.
    assert check_implication(ok_sup, sub) is None

    # --- ladder: a violation between non-adjacent rungs must be caught ---
    ladder = [
        Quote("T3.2", yes_bid_c=30, yes_bid_sz=10, yes_ask_c=32, yes_ask_sz=10),  # narrowest
        Quote("T3.1", yes_bid_c=33, yes_bid_sz=10, yes_ask_c=35, yes_ask_sz=10),
        Quote("T3.0", yes_bid_c=26, yes_bid_sz=10, yes_ask_c=28, yes_ask_sz=10),  # widest
    ]
    found = check_ladder(ladder, set_name="cpi")
    # Adjacent steps 0->1 are fine (30 bid vs 35 ask). The breaks are against the
    # widest rung, whose 28 ask sits below both narrower bids.
    edges = sorted(v.edge_c for v in found)
    assert edges == [2.0, 5.0], edges

    # --- partition, exhaustive: both bounds apply ---
    cheap = [Quote(f"B{i}", yes_bid_c=28, yes_bid_sz=5, yes_ask_c=30, yes_ask_sz=5) for i in range(3)]
    found = check_partition(cheap, exhaustive=True, set_name="buckets")
    assert len(found) == 1 and found[0].relation == "partition_underround"
    assert found[0].edge_c == 10 and found[0].max_size == 5  # asks sum to 90c

    rich = [Quote(f"B{i}", yes_bid_c=36, yes_bid_sz=4, yes_ask_c=38, yes_ask_sz=4) for i in range(3)]
    found = check_partition(rich, exhaustive=True, set_name="buckets")
    assert len(found) == 1 and found[0].relation == "partition_overround"
    assert found[0].edge_c == 8  # bids sum to 108c

    # --- partition, exclusive but NOT exhaustive: only the upper bound holds ---
    # This is the case that manufactures fake arbitrage if handled carelessly.
    # Asks summing to 90c is correct pricing for a 10% chance of "none of these".
    found = check_partition(cheap, exhaustive=False, set_name="buckets")
    assert found == [], "underround must not be flagged without exhaustiveness"
    found = check_partition(rich, exhaustive=False, set_name="buckets")
    assert len(found) == 1 and found[0].relation == "partition_overround"

    # A fairly priced exhaustive set produces nothing either way.
    fair = [Quote(f"B{i}", yes_bid_c=32, yes_bid_sz=5, yes_ask_c=34, yes_ask_sz=5) for i in range(3)]
    assert check_partition(fair, exhaustive=True) == []
    print("relations ok")


def test_linkage() -> None:
    def mkt(ticker, strike_type, floor=None, cap=None):
        return {"ticker": ticker, "strike_type": strike_type,
                "floor_strike": floor, "cap_strike": cap}

    # --- intervals read off the metadata, not off the title text ---
    assert market_interval(mkt("A", "greater", floor=3.0)) == Interval("A", 3.0, math.inf, "greater")
    assert market_interval(mkt("B", "less", cap=2.9)) == Interval("B", -math.inf, 2.9, "less")
    assert market_interval(mkt("C", "between", floor=2.9, cap=3.0)).high == 3.0
    # Strike types whose settlement we cannot read are skipped, not guessed at.
    assert market_interval(mkt("D", "functional")) is None
    assert market_interval(mkt("E", "greater")) is None  # required bound missing

    # --- implication direction on a "greater" ladder ---
    ivs = [market_interval(mkt(f"T{s}", "greater", floor=s)) for s in (3.0, 3.1, 3.2)]
    pairs = implication_pairs(ivs)
    # A higher threshold is the harder event, so it entails every lower one.
    assert ("T3.2", "T3.0") in pairs and ("T3.1", "T3.0") in pairs
    assert ("T3.0", "T3.1") not in pairs  # never the other way round
    assert len(pairs) == 3

    # --- exhaustiveness, shared-edge convention ---
    shared = [Interval("a", -math.inf, 2.9, "less"), Interval("b", 2.9, 3.0, "between"),
              Interval("c", 3.0, math.inf, "greater")]
    assert tiles_the_line(shared) is True
    # --- offset-grid convention: contiguous in intent, one grid step apart ---
    offset = [Interval("a", -math.inf, 2.9, "less"), Interval("b", 3.0, 3.1, "between"),
              Interval("c", 3.2, math.inf, "greater")]
    assert tiles_the_line(offset) is True
    # --- a genuinely missing bucket is a two-step gap and must be rejected ---
    missing = [Interval("a", -math.inf, 2.9, "less"), Interval("b", 3.2, math.inf, "greater")]
    assert tiles_the_line(missing) is False
    # --- open at one end, so some outcome is uncovered ---
    assert tiles_the_line([Interval("a", 2.9, 3.0, "between"),
                           Interval("b", 3.0, math.inf, "greater")]) is False
    # --- overlapping buckets could pay out together ---
    assert tiles_the_line([Interval("a", -math.inf, 3.1, "less"),
                           Interval("b", 3.0, math.inf, "greater")]) is False

    # --- end to end: an exclusive, tiling event is an exhaustive partition ---
    event = {"event_ticker": "CPI-26MAY", "mutually_exclusive": True}
    markets = [mkt("a", "less", cap=2.9), mkt("b", "between", floor=2.9, cap=3.0),
               mkt("c", "greater", floor=3.0)]
    sets = infer_sets(event, markets)
    assert len(sets) == 1 and sets[0].relation == "partition" and sets[0].exhaustive is True

    # --- exclusive with a hole: falls back to the weaker bound ---
    holey = [mkt("a", "less", cap=2.9), mkt("b", "greater", floor=3.9)]
    sets = infer_sets(event, holey)
    assert len(sets) == 1 and sets[0].exhaustive is False

    # --- Kalshi's flag contradicted by the geometry: refuse both bounds ---
    contradictory = [mkt("a", "less", cap=3.1), mkt("b", "greater", floor=3.0)]
    sets = infer_sets(event, contradictory)
    assert len(sets) == 1 and sets[0].exhaustive is False and "SKIP" in sets[0].notes

    # --- non-exclusive event with nested strikes is a ladder, ordered narrow to wide ---
    ladder_event = {"event_ticker": "CPI-26MAY-LADDER", "mutually_exclusive": False}
    rungs = [mkt(f"T{s}", "greater", floor=s) for s in (3.0, 3.1, 3.2)]
    sets = infer_sets(ladder_event, rungs)
    assert len(sets) == 1 and sets[0].relation == "ladder"
    assert sets[0].tickers == ["T3.2", "T3.1", "T3.0"]

    # --- an event with nothing readable produces nothing, rather than a guess ---
    assert infer_sets(ladder_event, [mkt("x", "custom"), mkt("y", "structured")]) == []
    print("linkage ok")


def test_fees() -> None:
    model = FeeModel()
    # 0.07 * 100 * 0.50 * 0.50 = $1.75 on 100 contracts at the worst price.
    assert model.taker_fee_cents(100, 50) == 175
    # Quadratic, so the tails are far cheaper: 0.07 * 100 * 0.05 * 0.95 = $0.3325.
    assert model.taker_fee_cents(100, 5) == 34  # rounded up to a whole cent
    # Symmetric in P and 1-P, which is why selling YES costs the same as buying
    # the NO it is equivalent to, and why the YES price alone determines the fee.
    assert model.taker_fee_cents(100, 45) == model.taker_fee_cents(100, 55)
    # Rounding is per ORDER, so small size pays proportionally more: a single
    # contract at 50c owes 1.75c in theory and is charged 2c.
    assert model.taker_fee_cents(1, 50) == 2
    assert model.taker_fee_cents(0, 50) == 0
    # The series multiplier scales it linearly.
    assert FeeModel(multiplier=2.0).taker_fee_cents(100, 50) == 350
    # A flat-schedule series must refuse rather than apply the wrong formula.
    try:
        FeeModel(fee_type="flat").taker_fee_cents(100, 50)
    except NotImplementedError:
        pass
    else:
        raise AssertionError("flat fee series should not silently use the quadratic formula")

    # The headline threshold: three legs around 33c cost ~4.65c per contract to
    # enter, so a partition summing to 98c is NOT a 2c opportunity.
    legs = [33, 33, 33]
    assert round_trip_threshold_cents(legs, 100, model) == 465
    assert net_edge_cents(2.0, legs, 100, model) == -265  # sum 98c: a loss
    assert net_edge_cents(5.0, legs, 100, model) == 35    # sum 95c: barely worth it
    # Maker fees are a fraction of taker, and zero on the plain quadratic series.
    assert model.maker_fee_cents(100, 50) == 0
    assert FeeModel(fee_type="quadratic_with_maker_fees").maker_fee_cents(100, 50) == 44
    print("fees ok")


def test_scanner() -> None:
    iset = InferredSet(name="buckets", relation="partition",
                       tickers=["A", "B", "C"], exhaustive=True)

    def q(ticker, bid, ask, size=5.0):
        return Quote(ticker, yes_bid_c=bid, yes_bid_sz=size, yes_ask_c=ask, yes_ask_sz=size)

    scanner = Scanner([iset], FeeModel())
    # Asks at 30 each sum to 90c on an exhaustive set: a 10c/contract underround.
    # Nothing can fire until all three legs have quotes.
    scanner.update(q("A", 28, 30), 1000)
    assert not scanner.open, "a set with missing legs must not be evaluated"
    scanner.update(q("B", 28, 30), 1100)
    assert not scanner.open
    scanner.update(q("C", 28, 30), 1200)
    assert len(scanner.open) == 1

    # More updates while it still violates extend the same episode rather than
    # opening new ones. This is the difference between counting opportunities
    # and counting book updates.
    scanner.update(q("A", 28, 30), 2000)
    scanner.update(q("B", 28, 30), 3000)
    assert len(scanner.open) == 1
    assert next(iter(scanner.open.values())).n_observations == 3

    # Prices correct one leg at a time. The episode must survive the partial
    # corrections: with asks at 34/30/30 the sum is still 94c, so the violation
    # is genuinely still standing. It closes only when the last leg moves and the
    # sum finally clears 100c.
    scanner.update(q("A", 32, 34), 4000)   # sum 94c, still violating
    scanner.update(q("B", 32, 34), 4100)   # sum 98c, still violating
    assert len(scanner.open) == 1
    scanner.update(q("C", 32, 34), 4200)   # sum 102c, bound holds again
    episodes = scanner.finish()
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep.closed is True
    assert ep.start_ms == 1200 and ep.end_ms == 4200
    assert ep.duration_ms == 3000
    # 10c/contract on 5 contracts = 50c gross; fees are 3 legs x ceil(0.07*5*0.3*0.7*100)
    # = 3 x 8c = 24c; net 26c, so this one clears.
    assert ep.peak_gross_edge_c == 10.0 and ep.peak_size == 5.0
    assert ep.fee_at_peak_c == 24 and ep.peak_net_c == 26
    assert ep.clears_fees is True

    # --- an episode still standing at the end of the data is not "closed" ---
    scanner = Scanner([iset], FeeModel())
    for t, tk in ((1000, "A"), (1100, "B"), (1200, "C")):
        scanner.update(q(tk, 28, 30), t)
    episodes = scanner.finish()
    assert len(episodes) == 1 and episodes[0].closed is False

    # --- a violation too small to cover fees is recorded but does not clear ---
    # Asks at 33 sum to 99c: a 1c/contract edge against 3 x 2c of fees on 5 lots.
    scanner = Scanner([iset], FeeModel())
    for t, tk in ((1000, "A"), (1100, "B"), (1200, "C")):
        scanner.update(q(tk, 31, 33), t)
    ep = scanner.finish()[0]
    assert ep.peak_gross_edge_c == 1.0
    # 5 contracts x 1c = 5c gross, against 3 legs x ceil(0.07*5*0.33*0.67*100)
    # = 3 x 8c = 24c of fees. A 19c loss dressed up as a 1c edge.
    assert ep.fee_at_peak_c == 24
    assert ep.peak_net_c == -19
    assert ep.clears_fees is False, "a sub-fee violation must not count as an opportunity"

    # --- episodes overlapping a feed gap are flagged, not dropped ---
    scanner = Scanner([iset], FeeModel(), gap_windows=[(1150, 1250)])
    for t, tk in ((1000, "A"), (1100, "B"), (1200, "C")):
        scanner.update(q(tk, 28, 30), t)
    assert scanner.finish()[0].spans_feed_gap is True

    # --- summarise reports the honest headline when nothing clears fees ---
    scanner = Scanner([iset], FeeModel())
    for t, tk in ((1000, "A"), (1100, "B"), (1200, "C")):
        scanner.update(q(tk, 31, 33), t)
    text = scanner_summarise(scanner.finish())
    assert "clear fees, so actually tradable: 0" in text
    assert "fee floor doing its job" in text
    assert "No no-arbitrage violations" in scanner_summarise([])
    print("scanner ok")


def test_calibration() -> None:
    rng = random.Random(11)

    def obs(p, outcome, event="E", ticker="T", hours=1.0):
        return Observation(ticker=ticker, event_ticker=event, ts_ms=0,
                           implied_prob=p, outcome=outcome, hours_to_resolution=hours)

    # --- Wilson interval behaves where the normal approximation does not ---
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0 and 0.0 < hi < 0.35, "must not extend below zero at the boundary"
    lo, hi = wilson_interval(10, 10)
    assert hi == 1.0 and 0.65 < lo < 1.0
    lo, hi = wilson_interval(50, 100)
    assert 0.40 < lo < 0.41 and 0.59 < hi < 0.60  # symmetric around 0.5
    # More data narrows it.
    assert (wilson_interval(500, 1000)[1] - wilson_interval(500, 1000)[0]) < (hi - lo)

    # --- Brier reference points ---
    coward = [obs(0.5, rng.randint(0, 1)) for _ in range(500)]
    assert abs(brier_score(coward) - 0.25) < 1e-12, "always saying 50% scores exactly 0.25"
    perfect = [obs(1.0, 1), obs(0.0, 0)]
    assert brier_score(perfect) == 0.0

    # --- the Murphy decomposition must be an identity, not an approximation ---
    sample = []
    for i in range(3000):
        p = rng.randint(1, 99) / 100          # whole-cent prices, as Kalshi quotes
        sample.append(obs(p, 1 if rng.random() < p else 0, event=f"E{i % 40}"))
    parts = brier_decomposition(sample)
    assert abs(parts.brier - (parts.reliability - parts.resolution + parts.uncertainty)) < 1e-12

    # Raw reliability is NOT near zero even for perfectly calibrated data: with
    # 99 possible prices and 3000 observations the finite-sample noise floor is
    # about 0.0055, and the raw number sits right on it. Comparing raw
    # reliability against zero would call an ideal market miscalibrated.
    assert 0.004 < parts.reliability < 0.008, parts.reliability
    assert abs(parts.reliability - parts.reliability_noise) < 0.002
    assert abs(parts.reliability_excess) < 0.002, "excess must be ~0 for honest prices"
    # Skill has a ceiling set by the question mix, not by forecaster quality.
    # With prices drawn uniformly on [0,1], even a perfect forecaster scores
    # E[p(1-p)] = 1/6 against an uncertainty of 1/4, so the best possible skill
    # is (0.25 - 1/6) / 0.25 = 1/3. Expecting more than that would be expecting
    # the forecaster to beat the irreducible randomness of the outcomes.
    assert 0.28 < parts.skill_vs_base_rate < 0.38, parts.skill_vs_base_rate
    assert abs(parts.brier - 1 / 6) < 0.02, "a calibrated forecaster's Brier is E[p(1-p)] = 1/6"

    curve = reliability_table(sample)
    for b in curve:
        assert abs(b.gap) < 0.12, f"bin {b.lo}-{b.hi} drifted: gap {b.gap}"

    # --- a biased market must be caught ---
    # Longshots overpriced: the market quotes p but the truth is lower at the top
    # end, which is the classic favourite-longshot pattern.
    biased = []
    for i in range(3000):
        p = rng.randint(1, 99) / 100
        truth = p ** 1.6                      # pulls low probabilities further down
        biased.append(obs(p, 1 if rng.random() < truth else 0, event=f"E{i % 40}"))
    bparts = brier_decomposition(biased)
    assert abs(bparts.brier - (bparts.reliability - bparts.resolution + bparts.uncertainty)) < 1e-12
    # The bias must survive subtracting the noise floor, which is the whole point
    # of computing it: it is evidence, not sampling scatter.
    assert bparts.reliability_excess > 0.01, bparts.reliability_excess
    assert bparts.reliability_excess > parts.reliability_excess * 10
    # Longshot bins should resolve yes LESS often than their price claims.
    longshots = [b for b in reliability_table(biased) if b.hi <= 0.3]
    assert all(b.gap < 0 for b in longshots), "longshots should underperform their price"

    # --- dependence between observations ---
    # 20 events, each priced differently and resolving as a single coin flip that
    # every market inside it shares. This is the shape real data has: one event
    # outcome, observed many times across its markets and across time.
    clustered = []
    for e in range(20):                        # 20 independent events
        p_e = rng.randint(10, 90) / 100
        outcome = 1 if rng.random() < p_e else 0
        for m in range(5):                     # 5 markets each, same event outcome
            for _ in range(10):                # each sampled 10 times
                clustered.append(obs(p_e, outcome, event=f"EV{e}", ticker=f"EV{e}-M{m}"))
    note = effective_sample_note(clustered)
    assert "1000 observations" in note and "100 distinct markets" in note and "20 independent" in note

    # Resampling events must give a wider interval than resampling rows, because
    # rows pretend correlated observations are independent.
    c_lo, c_hi = clustered_bootstrap_brier(clustered, n_resamples=300, seed=3)
    naive = sorted(
        brier_score([rng.choice(clustered) for _ in range(len(clustered))])
        for _ in range(300)
    )
    naive_width = naive[int(0.975 * 300)] - naive[int(0.025 * 300)]
    assert (c_hi - c_lo) > naive_width * 2, (
        "clustering must widen the interval; if it does not, the sample size is being overstated"
    )

    # --- horizon split ---
    horizons = by_horizon(
        [obs(0.5, 1, hours=h) for h in (0.5, 2, 2, 100, 100, 100)],
        horizons=[(0, 1), (1, 6), (6, 10**6)],
    )
    assert [label for label, _ in horizons] == ["0-1h", "1-6h", ">6h"]
    assert [p.n for _, p in horizons] == [1, 2, 3]

    # --- empty input must not raise ---
    assert brier_decomposition([]).n == 0
    assert "No resolved observations" in summarise([])
    assert "reliability curve" in summarise(sample)
    print("calibration ok")


def main() -> int:
    tmp = Path(__file__).resolve().parents[1] / "data" / "_selftest"
    tmp.mkdir(parents=True, exist_ok=True)
    for stale in tmp.glob("selftest.sqlite*"):
        stale.unlink()

    test_prices()
    test_book()
    test_sequence()
    test_signing()
    test_relations()
    test_linkage()
    test_fees()
    test_scanner()
    test_calibration()
    asyncio.run(test_bucket())
    test_storage(tmp)
    print("\nall offline tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
