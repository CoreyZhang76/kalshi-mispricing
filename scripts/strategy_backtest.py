"""Walk-forward test of one trading rule on resolved markets.

    .venv/bin/python scripts/strategy_backtest.py --db data/calibration.sqlite --series KXHIGHNY

The rule: train a classifier on past events only. For each new market, if the
classifier says YES is more likely than the price, and the price is 49c or below,
buy one YES contract at the first snapshot where that holds. Profit per trade is
payoff minus price, minus Kalshi's taker fee, minus a 1c half-spread (the stored
prices are mids, and buying at the mid is not possible).

Rules this script keeps:
  - No lookahead. A test month is only predicted by a model trained on events
    that closed before that month started.
  - One trade per market. Hourly snapshots of one market share one outcome.
  - The uncertainty interval resamples EVENTS, since markets in one event resolve
    together, not rows or trades.

It also reports a baseline: buying YES on every market priced 49c or below with
no model at all. If the model's trades don't beat that baseline, the classifier
added nothing and the edge is just the price band.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kmm.calibration import Observation
from kmm.fees import BASE_FEE_RATE
from scripts.fetch_resolved import load  # noqa: E402

PRICE_CAP = 0.49
HALF_SPREAD = 0.01
MIN_TRAIN_EVENTS = 15


def taker_fee(price: float) -> float:
    """Kalshi's taker fee per contract, in dollars, before the per-order rounding."""
    return BASE_FEE_RATE * price * (1.0 - price)


def features(o: Observation) -> np.ndarray:
    p = min(max(o.implied_prob, 1e-3), 1 - 1e-3)
    return np.array([1.0, math.log(p / (1 - p)), math.log1p(o.hours_to_resolution)])


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 200) -> np.ndarray:
    """Plain Newton-step logistic regression with a small ridge penalty."""
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        z = X @ w
        p = 1.0 / (1.0 + np.exp(-z))
        grad = X.T @ (p - y) + l2 * np.r_[0.0, w[1:]]
        hess = (X * (p * (1 - p))[:, None]).T @ X + l2 * np.diag(np.r_[0.0, np.ones(len(w) - 1)])
        step = np.linalg.solve(hess + 1e-9 * np.eye(len(w)), grad)
        w -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w


def month_key(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m")


def walk_forward(obs: list[Observation]) -> list[dict]:
    """One row per market that the rule traded: the first snapshot where it fires."""
    event_close: dict[str, int] = {}
    for o in obs:
        event_close[o.event_ticker] = max(event_close.get(o.event_ticker, 0), o.ts_ms)

    by_month: dict[str, list[Observation]] = defaultdict(list)
    for o in obs:
        by_month[month_key(o.ts_ms)].append(o)

    trades: list[dict] = []
    for month in sorted(by_month):
        month_start = min(o.ts_ms for o in by_month[month])
        train = [o for o in obs if event_close[o.event_ticker] < month_start]
        train_events = {o.event_ticker for o in train}
        if len(train_events) < MIN_TRAIN_EVENTS:
            continue
        w = fit_logistic(np.array([features(o) for o in train]),
                         np.array([o.outcome for o in train], dtype=float))

        fired: set[str] = set()
        for o in sorted(by_month[month], key=lambda x: x.ts_ms):
            if o.ticker in fired or o.implied_prob > PRICE_CAP:
                continue
            q = 1.0 / (1.0 + math.exp(-float(features(o) @ w)))
            if q <= o.implied_prob + HALF_SPREAD + taker_fee(o.implied_prob):
                continue
            fired.add(o.ticker)
            cost = o.implied_prob + HALF_SPREAD
            trades.append({
                "ticker": o.ticker, "event": o.event_ticker, "month": month,
                "price": o.implied_prob, "model_q": q, "outcome": o.outcome,
                "pnl": o.outcome - cost - taker_fee(o.implied_prob),
            })
    return trades


def baseline(obs: list[Observation]) -> list[dict]:
    """No model: buy YES at the first snapshot priced at or below the cap."""
    first: dict[str, Observation] = {}
    for o in sorted(obs, key=lambda x: x.ts_ms):
        if o.implied_prob <= PRICE_CAP and o.ticker not in first:
            first[o.ticker] = o
    return [{"ticker": o.ticker, "event": o.event_ticker, "price": o.implied_prob,
             "outcome": o.outcome,
             "pnl": o.outcome - (o.implied_prob + HALF_SPREAD) - taker_fee(o.implied_prob)}
            for o in first.values()]


def summarise(name: str, rows: list[dict], rng: np.random.Generator) -> None:
    if not rows:
        print(f"{name}: no trades")
        return
    pnl = np.array([r["pnl"] for r in rows])
    by_event: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_event[r["event"]].append(r["pnl"])
    events = list(by_event)
    boots = []
    for _ in range(2000):
        pick = rng.choice(len(events), size=len(events), replace=True)
        boots.append(np.mean([p for i in pick for p in by_event[events[i]]]))
    lo, hi = np.percentile(boots, [2.5, 97.5])
    print(f"{name}: {len(rows)} trades across {len(events)} events")
    print(f"  mean net P&L per $1 contract  {pnl.mean():+.4f}   (95% CI over events [{lo:+.4f}, {hi:+.4f}])")
    print(f"  hit rate {np.mean([r['outcome'] for r in rows]):.3f}   mean price {np.mean([r['price'] for r in rows]):.3f}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("data/calibration.sqlite"))
    parser.add_argument("--series", required=True)
    args = parser.parse_args()

    obs = load(args.db, args.series)
    print(f"{len(obs)} observations, {len({o.event_ticker for o in obs})} events, "
          f"{len({o.ticker for o in obs})} markets in {args.series}\n")
    rng = np.random.default_rng(0)
    summarise("model rule (walk-forward)", walk_forward(obs), rng)
    print()
    summarise("baseline: buy YES at <=49c, no model", baseline(obs), rng)


if __name__ == "__main__":
    main()
