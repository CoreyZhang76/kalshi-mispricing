"""Walk-forward test using cross-market prices inside Kalshi.

    .venv/bin/python scripts/cross_market_backtest.py --series KXMLBGAME

Every input comes from stored Kalshi prices of resolved markets. Nothing outside
Kalshi is used.

Features, all known before the trade:
  - the logit of the price at the snapshot
  - log hours to resolution
  - the event overround: the sum of YES prices across every market in the same
    event at the same timestamp, minus 1. A mutually exclusive set should sum to
    about 1, so a positive overround means the event's prices are collectively rich
    and a negative one means they are collectively cheap.

Snapshots are only used when at least two markets in the event share the timestamp,
so the overround is measured rather than assumed.

The rule is the same as kalshi_only_backtest.py: buy one YES contract on the first
snapshot where the classifier's probability exceeds the price plus Kalshi's taker
fee and a 1c half-spread, with the price at 49c or below. Walk-forward by month, one
trade per market, event-clustered intervals.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from kmm.fees import BASE_FEE_RATE  # noqa: E402
from scripts.kalshi_only_backtest import report  # noqa: E402
from scripts.strategy_backtest import fit_logistic  # noqa: E402

PRICE_CAP = 0.49
HALF_SPREAD = 0.01
MIN_TRAIN_EVENTS = 15
MIN_SIBLINGS = 2


def logit(p: float) -> float:
    p = min(max(p, 1e-3), 1 - 1e-3)
    return math.log(p / (1 - p))


def load_with_overround(db: Path, series: str) -> tuple[list[dict], int]:
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT ticker, event_ticker, ts_ms, implied_prob, outcome, hours_to_res "
        "FROM calibration_obs WHERE series_ticker = ?",
        (series,),
    ).fetchall()
    conn.close()

    # Sum and count the YES prices of every market in an event at each timestamp.
    total: dict[tuple[str, int], float] = defaultdict(float)
    count: dict[tuple[str, int], int] = defaultdict(int)
    for ticker, event, ts, price, _outcome, _hours in rows:
        total[(event, ts)] += price
        count[(event, ts)] += 1

    out: list[dict] = []
    skipped = 0
    for ticker, event, ts, price, outcome, hours in rows:
        key = (event, ts)
        if count[key] < MIN_SIBLINGS:
            skipped += 1
            continue
        out.append({"ticker": ticker, "event": event, "ts": ts, "price": price,
                    "outcome": outcome, "hours": hours,
                    "overround": total[key] - 1.0})
    return out, skipped


def features(o: dict) -> np.ndarray:
    return np.array([1.0, logit(o["price"]), math.log1p(o["hours"]), o["overround"]])


def month_of(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m")


def walk_forward(obs: list[dict]) -> list[dict]:
    event_close: dict[str, int] = {}
    for o in obs:
        event_close[o["event"]] = max(event_close.get(o["event"], 0), o["ts"])
    by_month: dict[str, list[dict]] = defaultdict(list)
    for o in obs:
        by_month[month_of(o["ts"])].append(o)

    trades: list[dict] = []
    for month in sorted(by_month):
        start = min(o["ts"] for o in by_month[month])
        train = [o for o in obs if event_close[o["event"]] < start]
        if len({o["event"] for o in train}) < MIN_TRAIN_EVENTS:
            continue
        w = fit_logistic(np.array([features(o) for o in train]),
                         np.array([o["outcome"] for o in train], dtype=float))
        fired: set[str] = set()
        for o in sorted(by_month[month], key=lambda z: z["ts"]):
            if o["ticker"] in fired or o["price"] > PRICE_CAP:
                continue
            q = 1 / (1 + math.exp(-float(features(o) @ w)))
            fee = BASE_FEE_RATE * o["price"] * (1 - o["price"])
            if q <= o["price"] + HALF_SPREAD + fee:
                continue
            fired.add(o["ticker"])
            trades.append({"event": o["event"], "price": o["price"], "outcome": o["outcome"],
                           "pnl": o["outcome"] - o["price"] - HALF_SPREAD - fee})
    return trades


def holdout(obs: list[dict], cutoff_ms: int) -> list[dict]:
    """Fit once on events that closed before the cutoff, then score only snapshots
    at or after it with those frozen weights. No refit, no tuning inside the window."""
    event_close: dict[str, int] = {}
    for o in obs:
        event_close[o["event"]] = max(event_close.get(o["event"], 0), o["ts"])
    train = [o for o in obs if event_close[o["event"]] < cutoff_ms]
    if len({o["event"] for o in train}) < MIN_TRAIN_EVENTS:
        return []
    w = fit_logistic(np.array([features(o) for o in train]),
                     np.array([o["outcome"] for o in train], dtype=float))
    test = [o for o in obs if o["ts"] >= cutoff_ms]
    fired: set[str] = set()
    trades: list[dict] = []
    for o in sorted(test, key=lambda z: z["ts"]):
        if o["ticker"] in fired or o["price"] > PRICE_CAP:
            continue
        q = 1 / (1 + math.exp(-float(features(o) @ w)))
        fee = BASE_FEE_RATE * o["price"] * (1 - o["price"])
        if q <= o["price"] + HALF_SPREAD + fee:
            continue
        fired.add(o["ticker"])
        trades.append({"event": o["event"], "price": o["price"], "outcome": o["outcome"],
                       "pnl": o["outcome"] - o["price"] - HALF_SPREAD - fee})
    print(f"frozen fit on {len({o['event'] for o in train})} events closing before cutoff; "
          f"weights {np.round(w, 3).tolist()}")
    return trades


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "calibration.sqlite")
    parser.add_argument("--series", required=True)
    parser.add_argument("--holdout-from", help="UTC date YYYY-MM-DD; enables frozen holdout")
    args = parser.parse_args()

    obs, skipped = load_with_overround(args.db, args.series)
    print(f"{args.series}: {len(obs)} usable snapshots over "
          f"{len({o['event'] for o in obs})} events; {skipped} skipped with fewer than "
          f"{MIN_SIBLINGS} markets at the same timestamp\n")
    if args.holdout_from:
        cutoff = datetime.strptime(args.holdout_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        report("Cross-market model (frozen holdout)",
               holdout(obs, int(cutoff.timestamp() * 1000)), np.random.default_rng(0))
        return
    report("Cross-market model (walk-forward)", walk_forward(obs),
           np.random.default_rng(0))


if __name__ == "__main__":
    main()
