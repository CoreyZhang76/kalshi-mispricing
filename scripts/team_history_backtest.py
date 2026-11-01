"""Walk-forward test of a team-history feature using only resolved Kalshi markets.

    .venv/bin/python -m scripts.team_history_backtest --series KXNHLGAME

Pre-registered before running. The feature for each snapshot is the team's
smoothed win rate in its earlier resolved Kalshi markets. Only markets whose event
closed before the current event began are counted, so nothing from the game itself
or later games leaks in. Nothing outside Kalshi is used.

Features:
  - logit of the YES price at the snapshot
  - logit of the smoothed historical win rate (prior of 0.5 with weight 5 games)
  - log1p hours to resolution

Rule, identical to the cross-market test: buy one YES contract when the classifier's
probability beats price + taker fee + 1c half-spread, with price at or below 49c.
Walk-forward by month, one trade per market, event-clustered intervals.
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
from scripts.cross_market_backtest import logit, month_of  # noqa: E402
from scripts.kalshi_only_backtest import report  # noqa: E402
from scripts.strategy_backtest import fit_logistic  # noqa: E402

PRICE_CAP = 0.49
HALF_SPREAD = 0.01
MIN_TRAIN_EVENTS = 15
PRIOR_RATE = 0.5
PRIOR_WEIGHT = 5.0


def load(db: Path, series: str) -> list[dict]:
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT ticker, event_ticker, ts_ms, implied_prob, outcome, hours_to_res "
        "FROM calibration_obs WHERE series_ticker = ?",
        (series,),
    ).fetchall()
    conn.close()
    return [{"ticker": t, "event": e, "ts": ts, "price": p, "outcome": y, "hours": h,
             "team": t.split("-")[-1]} for t, e, ts, p, y, h in rows]


def add_team_history(obs: list[dict]) -> list[dict]:
    """Attach each snapshot's team prior, using only events that closed before it began."""
    # One resolved outcome per market, with the time its event closed.
    resolved: dict[str, tuple[str, int, int]] = {}
    for o in obs:
        cur = resolved.get(o["ticker"])
        close = max(cur[2], o["ts"]) if cur else o["ts"]
        resolved[o["ticker"]] = (o["team"], o["outcome"], close)
    games_by_team: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for team, outcome, close in resolved.values():
        games_by_team[team].append((close, outcome))
    for team in games_by_team:
        games_by_team[team].sort()

    event_start: dict[str, int] = {}
    for o in obs:
        event_start[o["event"]] = min(event_start.get(o["event"], o["ts"]), o["ts"])

    out = []
    for o in obs:
        history = [y for close, y in games_by_team[o["team"]] if close < event_start[o["event"]]]
        n = len(history)
        rate = (sum(history) + PRIOR_RATE * PRIOR_WEIGHT) / (n + PRIOR_WEIGHT)
        out.append({**o, "team_rate": rate, "n_history": n})
    return out


def features(o: dict) -> np.ndarray:
    return np.array([1.0, logit(o["price"]), logit(o["team_rate"]), math.log1p(o["hours"])])


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "calibration.sqlite")
    parser.add_argument("--series", required=True)
    args = parser.parse_args()

    obs = add_team_history(load(args.db, args.series))
    covered = [o for o in obs if o["n_history"] >= 1]
    print(f"{args.series}: {len(obs)} snapshots over {len({o['event'] for o in obs})} events; "
          f"{len(covered)} with at least one prior game for the team\n")
    report("Team-history model (walk-forward)", walk_forward(obs), np.random.default_rng(0))


if __name__ == "__main__":
    main()
