"""Calibration analysis on generated data, so the charts can be seen without a key.

    .venv/Scripts/python.exe scripts/demo_calibration.py

Generates two synthetic markets and runs the full analysis on each:

  honest-quotes       prices are unbiased estimates of the true probability
  favourite-longshot  quotes are compressed toward 50c, so longshots are
                      overpriced and favourites underpriced

Running both matters. A calibration pipeline that only ever sees good data cannot
tell you whether it would DETECT bias, and "the market looks fine" is a much less
convincing finding from a tool that has never been shown to report anything else.

As with the scanner demo, this is not a result about Kalshi. The bias in the
second dataset is one I put there.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fetch_resolved import write as write_observations  # sibling script, same directory
from kmm.calibration import Observation, brier_decomposition, summarise
from kmm.plots import horizon_diagram, reliability_diagram

FIGURES = Path(__file__).resolve().parents[1] / "reports" / "figures"
DEMO_DB = Path(__file__).resolve().parents[1] / "data" / "demo_calibration.sqlite"
HORIZONS = [0.5, 3, 12, 48, 120, 400]
RESOLUTION_MS = 1_780_000_000_000  # notional settlement time for the synthetic set


def generate(n_events: int, shrink: float, seed: int) -> list[Observation]:
    """Build observations with a known amount of favourite-longshot bias.

    The bias is applied by compressing quotes toward 50c:

        quoted = 0.5 + (true_p - 0.5) * shrink

    `shrink` of 1.0 is honest pricing. Below 1.0 the market is too timid at both
    ends: a true 10% is quoted at 22c and a true 90% at 78c. That produces the
    textbook favourite-longshot signature, where the reliability curve CROSSES
    the diagonal, sagging below it among longshots (overpriced, they win less
    often than quoted) and rising above it among favourites (underpriced).

    A uniform downward shift is a different thing and would be easy to confuse
    with this one. Compressing toward the middle is what a preference for lottery
    tickets actually looks like in the data.

    The structure mirrors real data: one outcome per market, observed repeatedly
    across the run-up to resolution, with markets grouped into events. Getting
    that structure right is what makes the clustered bootstrap meaningful.
    """
    rng = random.Random(seed)
    obs: list[Observation] = []
    for e in range(n_events):
        for m in range(4):
            true_p = rng.random()
            outcome = 1 if rng.random() < true_p else 0
            base_quote = 0.5 + (true_p - 0.5) * shrink
            for hours in HORIZONS:
                # The quote wanders toward the truth as resolution approaches,
                # which is what a market that is learning looks like.
                noise = rng.gauss(0, 0.18 * min(1.0, hours / 48))
                quoted = min(0.99, max(0.01, base_quote + noise))
                obs.append(
                    Observation(
                        ticker=f"EV{e}-M{m}", event_ticker=f"EV{e}",
                        # Distinct per horizon. Writing 0 for every row would let
                        # the (ticker, ts_ms) primary key in calibration_obs
                        # collapse all six observations of a market into one.
                        ts_ms=int((RESOLUTION_MS - hours * 3_600_000)),
                        implied_prob=round(quoted, 2),  # whole cents, as Kalshi quotes
                        outcome=outcome, hours_to_resolution=hours,
                    )
                )
    return obs


def report(name: str, obs: list[Observation]) -> None:
    print(f"\n{'=' * 72}\n{name}\n{'=' * 72}")
    print(summarise(obs))

    rel = reliability_diagram(obs, FIGURES / f"reliability_{name}.png",
                              title=f"Reliability: {name} (synthetic)")
    hor = horizon_diagram(obs, FIGURES / f"horizon_{name}.png",
                          title=f"Score by time to resolution: {name} (synthetic)")
    print(f"\nfigures: {rel.name}, {hor.name}")


def main() -> None:
    good = generate(n_events=120, shrink=1.0, seed=4)
    biased = generate(n_events=120, shrink=0.65, seed=4)

    print("NOTE: generated data. Any bias shown is bias this script introduced.")

    # Persist the biased set so build_report.py has something to report on. The
    # biased one rather than the honest one, because a report generator that has
    # only ever rendered a clean result has not been shown to render anything else.
    if DEMO_DB.exists():
        DEMO_DB.unlink()
    write_observations(DEMO_DB, biased, "DEMO-SYNTHETIC")
    report("honest-quotes", good)
    report("favourite-longshot", biased)

    g, b = brier_decomposition(good), brier_decomposition(biased)
    print(f"\n{'=' * 72}\nside by side\n{'=' * 72}")
    print(f"{'':>22}{'calibrated':>14}{'biased':>14}")
    print(f"{'Brier':>22}{g.brier:>14.4f}{b.brier:>14.4f}")
    print(f"{'raw reliability':>22}{g.reliability:>14.4f}{b.reliability:>14.4f}")
    print(f"{'noise floor':>22}{g.reliability_noise:>14.4f}{b.reliability_noise:>14.4f}")
    print(f"{'excess reliability':>22}{g.reliability_excess:>+14.4f}{b.reliability_excess:>+14.4f}")
    print(f"{'resolution':>22}{g.resolution:>14.4f}{b.resolution:>14.4f}")
    print("\nThe excess reliability row is the one that separates them. Raw "
          "reliability is nonzero for both\nbecause of finite-sample noise, so "
          "comparing it against zero would call the honest market biased too.")
    print("\nTwo things to read carefully in the curves above.")
    print("  1. The biased set CROSSES the diagonal: negative gaps from 0.2 to 0.5, "
          "positive from\n     0.5 to 0.9. That crossing is the favourite-longshot "
          "signature. A uniform shift in one\n     direction would be a different "
          "problem with a different cause.")
    print("  2. Both sets drift at the very ends, in the direction OPPOSITE to the "
          "injected bias.\n     That is regression to the mean, not miscalibration: a "
          "quote near 99c is more often a\n     noisy read on something lower than a "
          "true 99%, and few observations reach the extremes\n     once quotes are "
          "compressed. Real data will show the same artefact, so the end bins are\n"
          "     the ones least safe to draw conclusions from.")


if __name__ == "__main__":
    main()
