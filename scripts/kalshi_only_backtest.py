"""Walk-forward test using only Kalshi data: prices, time, and outcomes.

    .venv/bin/python scripts/kalshi_only_backtest.py --series KXMLBGAME

Every input comes from the stored Kalshi price histories of resolved markets.
Nothing outside Kalshi is used.

Features, all known before the trade:
  - the logit of the price at the snapshot
  - log hours to resolution
  - the change in logit price over the previous six hours (price momentum)

The rule is fixed in advance and not tuned on the data: buy one YES contract on
the first snapshot where the classifier's probability exceeds the price plus
Kalshi's taker fee and a 1c half-spread, and the price is 49c or below.

Test discipline:
  - Walk-forward by month: each month is predicted by a model fit only on events
    that closed before it began.
  - One trade per market.
  - Confidence intervals resample events, since markets in one event resolve together.
  - Snapshots without a price six hours earlier are skipped in training and testing
    alike, and the skipped count is printed.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from kmm.fees import BASE_FEE_RATE  # noqa: E402
from scripts.strategy_backtest import fit_logistic  # noqa: E402

PRICE_CAP = 0.49
HALF_SPREAD = 0.01
LAG_MS = 6 * 3600 * 1000
LAG_TOLERANCE_MS = 90 * 60 * 1000
MIN_TRAIN_EVENTS = 15


def logit(p: float) -> float:
    p = min(max(p, 1e-3), 1 - 1e-3)
    return math.log(p / (1 - p))


def load_with_momentum(db: Path, series: str) -> tuple[list[dict], int]:
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT ticker, event_ticker, ts_ms, implied_prob, outcome, hours_to_res "
        "FROM calibration_obs WHERE series_ticker = ? ORDER BY ticker, ts_ms",
        (series,),
    ).fetchall()
    conn.close()

    by_ticker: dict[str, list[tuple]] = defaultdict(list)
    for r in rows:
        by_ticker[r[0]].append(r)

    out: list[dict] = []
    skipped = 0
    for ticker, series_rows in by_ticker.items():
        times = [r[2] for r in series_rows]
        for ticker_, event, ts, price, outcome, hours in series_rows:
            i = bisect_right(times, ts - LAG_MS) - 1
            if i < 0 or ts - times[i] > LAG_MS + LAG_TOLERANCE_MS:
                skipped += 1
                continue
            momentum = logit(price) - logit(series_rows[i][3])
            out.append({"ticker": ticker, "event": event, "ts": ts, "price": price,
                        "outcome": outcome, "momentum": momentum, "hours": hours})
    return out, skipped


def features(o: dict) -> np.ndarray:
    return np.array([1.0, logit(o["price"]), math.log1p(o["hours"]), o["momentum"]])


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


def report(name: str, trades: list[dict], rng: np.random.Generator) -> None:
    if not trades:
        print(f"{name}: no trades")
        return
    by_event: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by_event[t["event"]].append(t["pnl"])
    events = list(by_event)
    boots = []
    for _ in range(2000):
        pick = rng.choice(len(events), size=len(events), replace=True)
        boots.append(np.mean([p for i in pick for p in by_event[events[i]]]))
    lo, hi = np.percentile(boots, [2.5, 97.5])
    pnl = np.array([t["pnl"] for t in trades])
    print(f"{name}: {len(trades)} trades across {len(events)} events")
    print(f"  mean net P&L per $1 contract  {pnl.mean():+.4f}   "
          f"(95% CI over events [{lo:+.4f}, {hi:+.4f}])")
    print(f"  hit rate {np.mean([t['outcome'] for t in trades]):.3f}   "
          f"mean price {np.mean([t['price'] for t in trades]):.3f}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "calibration.sqlite")
    parser.add_argument("--series", required=True)
    args = parser.parse_args()

    obs, skipped = load_with_momentum(args.db, args.series)
    print(f"{args.series}: {len(obs)} usable snapshots over "
          f"{len({o['event'] for o in obs})} events; {skipped} skipped for no 6h-earlier price\n")
    report("Kalshi-only model (walk-forward)", walk_forward(obs),
           np.random.default_rng(0))


if __name__ == "__main__":
    main()
