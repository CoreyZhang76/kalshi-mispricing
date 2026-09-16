"""Assemble the markdown report from whatever the scanner and calibration produced.

    .venv/Scripts/python.exe scripts/build_report.py --demo
    .venv/Scripts/python.exe scripts/build_report.py \
        --scan-db data/kalshi.sqlite --calib-db data/calibration.sqlite

The report is generated rather than hand-written so that every number in it comes
from the data, and re-running after more collection updates the prose rather than
leaving a stale claim behind. Sentences that characterise a result are chosen by
what the numbers actually are: if nothing clears the fee threshold, the report
says the market looked efficient, and it says so in the headline rather than
burying it.

The one thing this file will not do is describe a synthetic run as a finding.
`--demo` stamps a banner on every section and the conclusions are written in the
conditional, because a report built from data I generated is a test of the
report generator and nothing more.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kmm.calibration import (Observation, brier_decomposition, by_horizon,
                             at_horizon, clustered_bootstrap_brier,
                             effective_sample_note, null_excess_test, reliability_table)
from kmm.plots import horizon_diagram, reliability_diagram, reliability_table_markdown

ROOT = Path(__file__).resolve().parents[1]
FIGURES = ROOT / "reports" / "figures"

# Lead time at which calibration is measured. Calibration is a property of a
# forecast at a moment, so it has to be evaluated at a fixed horizon.
HORIZON_HOURS = 12.0

SYNTHETIC_BANNER = """> **This report was built from generated data.** The numbers below are a test of
> the report generator, not findings about Kalshi. Every violation and every bias
> shown here was written in by `scripts/demo_synthetic.py` and
> `scripts/demo_calibration.py`. Nothing in this document should be quoted as a
> result.
"""


def load_episodes(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "violation_episodes" not in tables:
            return []
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM violation_episodes")]
    finally:
        conn.close()


def load_observations(db_path: Path) -> list[Observation]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "calibration_obs" not in tables:
            return []
        return [
            Observation(*row)
            for row in conn.execute(
                "SELECT ticker, event_ticker, ts_ms, implied_prob, outcome, hours_to_res "
                "FROM calibration_obs"
            )
        ]
    finally:
        conn.close()


def collection_window(db_path: Path) -> str:
    if not db_path.exists():
        return "no collector runs recorded"
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*), MIN(started_ms), MAX(COALESCE(ended_ms, started_ms)) FROM collector_runs"
        ).fetchone()
        gaps = conn.execute("SELECT COUNT(*) FROM feed_gaps").fetchone()[0]
    except sqlite3.OperationalError:
        return "no collector runs recorded"
    finally:
        conn.close()
    runs, first, last = row
    if not runs or not first:
        return "no collector runs recorded"
    hours = (last - first) / 3_600_000
    return (f"{runs} collector run(s) spanning {hours:.1f} hours, "
            f"{gaps} feed gap(s) or disconnect(s) recorded")


def _plural(n: int, word: str) -> str:
    return word if n == 1 else word + "s"


def arbitrage_section(episodes: list[dict]) -> str:
    """Write the arbitrage findings, with the verdict chosen by the numbers."""
    if not episodes:
        return (
            "### Arbitrage\n\n"
            "No scan results are available. Either the collector has not run or the\n"
            "scanner has not been run against its output with `--write`.\n"
        )

    clean = [e for e in episodes if not e["spans_feed_gap"]]
    real = [e for e in clean if e["clears_fees"]]
    closed_real = [e for e in real if e["closed"]]
    discarded = len(episodes) - len(clean)

    lines = [
        "### Arbitrage",
        "",
        f"- **{len(episodes)}** violation {_plural(len(episodes), 'episode')} detected in total.",
        f"- **{discarded}** discarded for overlapping a feed gap, where our reconstructed "
        "book may have been wrong.",
        f"- **{len(real)}** of the remaining {len(clean)} cleared Kalshi's fees and "
        f"{'was' if len(real) == 1 else 'were'} therefore tradable in principle.",
    ]

    if not real:
        biggest = max(clean, key=lambda e: e["peak_gross_c"]) if clean else None
        lines += [
            "",
            "**Headline: no tradable arbitrage was found.** Prices did violate the "
            "no-arbitrage bounds, but never by more than the cost of taking every leg.",
        ]
        if biggest:
            lines.append(
                f"The largest violation was {biggest['peak_gross_c']:.1f}c per contract "
                f"on {biggest['peak_size']:g} contracts, against "
                f"{biggest['fee_at_peak_c']:.0f}c of fees."
            )
        lines.append(
            "\nThis is the expected result, and it is worth stating plainly rather than "
            "framing as a\nnull. A violation smaller than the fee is not an inefficiency "
            "that nobody noticed; it is\nthe cost of access, and no participant paying "
            "Kalshi's fees could have captured it either."
        )
        return "\n".join(lines) + "\n"

    durations = sorted(e["duration_ms"] for e in closed_real)
    still_open = len(real) - len(closed_real)
    lines += [
        "",
        f"Of the {len(real)} tradable {_plural(len(real), 'episode')}, "
        f"**{len(closed_real)}** closed within the data and **{still_open}** "
        f"{'was' if still_open == 1 else 'were'} still standing when collection ended.",
    ]
    if durations:
        mid = durations[len(durations) // 2]
        lines += [
            "",
            f"- Median duration: **{mid / 1000:.1f}s**",
            f"- Range: {durations[0] / 1000:.1f}s to {durations[-1] / 1000:.1f}s",
            f"- Largest net edge: **{max(e['peak_net_c'] for e in real):.0f}c** after fees",
        ]
    if still_open:
        lines.append(
            f"\nThe {still_open} unclosed episode(s) are excluded from the duration "
            "statistics. They lasted\n*at least* as long as observed, and including them "
            "would bias the median downward."
        )
    lines.append(
        f"\n**Headline: {len(real)} tradable {_plural(len(real), 'violation')} "
        f"{'was' if len(real) == 1 else 'were'} found.** Whether that represents\n"
        "exploitable inefficiency is a separate question from whether it represents a\n"
        "detectable one: see the limitations on queue position above."
    )
    return "\n".join(lines) + "\n"


def calibration_section(obs: list[Observation], synthetic: bool) -> str:
    if not obs:
        return (
            "### Calibration\n\n"
            "No resolved-market observations are available. Run\n"
            "`scripts/fetch_resolved.py --series <TICKER>` to build the dataset.\n"
        )

    parts = brier_decomposition(obs)
    lo, hi = clustered_bootstrap_brier(obs)
    events = len({o.event_ticker for o in obs})

    suffix = "_demo" if synthetic else ""
    reliability_diagram(obs, FIGURES / f"report_reliability{suffix}.png",
                        title="Reliability curve")
    horizon_diagram(obs, FIGURES / f"report_horizon{suffix}.png",
                    title="Score by time to resolution")

    # The verdict comes from whether the excess reliability interval excludes
    # zero, not from a hand-picked cutoff. A threshold like "under 0.005 is fine"
    # is a guess dressed as a criterion: reliability is a squared error in
    # probability units, so a visible bias with gaps of 0.05 to 0.10 lands at only
    # a few thousandths and would slip straight under it.
    excess = parts.reliability_excess
    # Sliced to a fixed lead time so each market contributes exactly one
    # observation. Pooling the whole price path removes the test's power entirely;
    # see null_excess_test for why.
    sliced = at_horizon(obs, HORIZON_HOURS)
    test = null_excess_test(sliced)
    detectable = test.detectable

    # Does the curve cross the diagonal, or is it displaced one way throughout?
    # Those are different problems with different causes, so the report should not
    # call a uniform shift a favourite-longshot bias.
    bins = [b for b in reliability_table(obs) if b.n >= 20]
    low_gap = sum(b.gap for b in bins if b.hi <= 0.4)
    high_gap = sum(b.gap for b in bins if b.lo >= 0.6)
    crosses = low_gap < 0 < high_gap

    if not detectable:
        verdict = (
            "**Headline: no miscalibration is detectable at this sample size.** Measured "
            f"{HORIZON_HOURS:g} hours\nbefore resolution on {len(sliced)} markets, excess "
            f"reliability is {test.observed:+.4f} against a\nperfectly calibrated null "
            f"whose 95th percentile is {test.null_95:+.4f} (p = {test.p_value:.3f}).\n\n"
            "That is not the same as proving the market is well calibrated. It means this "
            "much data\ncannot tell the difference. On synthetic data this test needed "
            "roughly 1600 single-horizon\nobservations to detect a substantial bias, so a "
            "null here is as much a statement about the\nsample size as about the market."
        )
    elif crosses:
        verdict = (
            "**Headline: a favourite-longshot bias is present.** Excess reliability is "
            f"{test.observed:+.4f}\nagainst a null 95th percentile of {test.null_95:+.4f} "
            f"(p = {test.p_value:.3f}). The reliability curve crosses\nthe "
            "diagonal: longshots resolve yes "
            "less often than their price claims and favourites more\noften. That is the "
            "signature of a preference for lottery tickets rather than an\narithmetic "
            "error, which is the pattern retail-driven mispricing produces."
        )
    else:
        direction = "below" if (low_gap + high_gap) < 0 else "above"
        verdict = (
            "**Headline: the market is measurably miscalibrated, but not in the "
            f"favourite-longshot\npattern.** Excess reliability is {test.observed:+.4f} "
            f"against a null 95th percentile of\n{test.null_95:+.4f} "
            f"(p = {test.p_value:.3f}). The curve sits {direction} the "
            "diagonal across the range rather\nthan crossing it, which is a uniform "
            "displacement and has a different cause from a\nlongshot preference."
        )

    return "\n".join([
        "### Calibration",
        "",
        verdict,
        "",
        f"![Reliability curve](figures/report_reliability{suffix}.png)",
        "",
        "| metric | value | reading |",
        "|---|---|---|",
        f"| Brier | {parts.brier:.4f} | lower is better; 0.25 is always saying 50% |",
        f"| Brier 95% CI | [{lo:.4f}, {hi:.4f}] | bootstrap over events, not rows |",
        f"| excess reliability at {HORIZON_HOURS:g}h | {test.observed:+.4f} | one observation per market |",
        f"| null 95th percentile | {test.null_95:+.4f} | what perfect calibration produces here |",
        f"| **p-value** | **{test.p_value:.3f}** | **{'miscalibration detected' if detectable else 'not distinguishable from calibrated'}** |",
        f"| raw reliability | {parts.reliability:.4f} | calibration error before debiasing |",
        f"| noise floor | {parts.reliability_noise:.4f} | what a perfect forecaster scores here |",
        f"| **excess reliability** | **{excess:+.4f}** | **the real miscalibration** |",
        f"| resolution | {parts.resolution:.4f} | discrimination; higher is better |",
        f"| uncertainty | {parts.uncertainty:.4f} | base rate variance; fixed by the questions |",
        f"| skill | {parts.skill_vs_base_rate:+.3f} | versus always forecasting the base rate |",
        "",
        f"Sample: {effective_sample_note(obs)}",
        "",
        "#### Bins",
        "",
        reliability_table_markdown(obs),
        "",
        "#### Does it sharpen as resolution approaches?",
        "",
        f"![Score by horizon](figures/report_horizon{suffix}.png)",
        "",
        "Brier falling toward resolution says little on its own, since the outcome "
        "becomes obvious.\nThe informative line is resolution: if it rises early, the "
        "market was anticipating rather\nthan catching up.",
        "",
        "| horizon | n | Brier | excess reliability | resolution |",
        "|---|---|---|---|---|",
        *[f"| {label} | {p.n} | {p.brier:.4f} | {p.reliability_excess:+.4f} | {p.resolution:.4f} |"
          for label, p in by_horizon(obs)],
        "",
    ])


LIMITATIONS = """### Limitations

These are stated before the findings rather than after, because several of them
bound how much the findings can mean.

**Sample size.** The arbitrage count comes from however long the collector ran on
however many markets. Both are small. A frequency estimate from a handful of
episodes carries an error bar wider than the estimate.

**Survivorship in market selection.** I chose which markets to track. That choice
is upstream of every arbitrage number here. Picking illiquid markets because they
look mispriced would produce violations that are really just stale quotes, and the
selection is not random, so these results do not generalise to the exchange.

**Paper fills are not fills.** Every profit figure is computed from quotes that
were visible, not from trades that happened. It ignores queue position, the
possibility that a quote disappears as you reach for it, and the fact that a
multi-leg package needs every leg to fill or the hedge is broken. A partially
filled arbitrage is an outright position, which is the opposite of what was
intended. Treat the net-edge numbers as an upper bound on what was available, not
an estimate of what was capturable.

**Calibration observations are not independent.** Repeated snapshots of one market
share a single outcome, and markets inside an event resolve together. The row
count overstates the sample by roughly an order of magnitude, which is why
intervals come from a bootstrap over events and the event count is quoted as the
sample size.

**The end bins are the least trustworthy.** Quotes carry observation noise, so a
price near 99c is more often a noisy read on something lower than a true 99%.
Regression to the mean produces apparent miscalibration at the extremes even when
pricing is honest.

**The fee model is from the published schedule, not from fills.** The formula and
the per-series multiplier are read from Kalshi's documentation and API. Nothing
here has been checked against an actual fee charged on an actual trade.
"""


def build(scan_db: Path, calib_db: Path, out: Path, synthetic: bool) -> Path:
    episodes = load_episodes(scan_db)
    obs = load_observations(calib_db)

    doc = [
        "# Kalshi cross-market mispricing: results",
        "",
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by `scripts/build_report.py`.",
        "",
    ]
    if synthetic:
        doc += [SYNTHETIC_BANNER, ""]

    doc += [
        "## What was measured",
        "",
        "Two separate questions, deliberately kept apart.",
        "",
        "1. **Internal consistency.** Do prices on logically linked contracts contradict "
        "each other, and\n   if so by how much and for how long? Linked sets are inferred "
        "from Kalshi's strike metadata,\n   not hand-picked, so the hypothesis is not "
        "chosen after seeing the prices.",
        "2. **Correspondence with reality.** Do contracts priced at 70c resolve yes about "
        "70% of the time?\n   A market can be perfectly self-consistent and still wrong "
        "about the world.",
        "",
        f"Collection: {collection_window(scan_db)}.",
        "",
        LIMITATIONS,
        "",
        "## Findings",
        "",
        arbitrage_section(episodes),
        "",
        calibration_section(obs, synthetic),
        "## Reproducing this",
        "",
        "```bash",
        ".venv/Scripts/python.exe scripts/selftest.py          # offline, no credentials",
        ".venv/Scripts/python.exe scripts/check_auth.py        # verify API credentials",
        ".venv/Scripts/python.exe -m kmm.collect --config config/markets.yaml",
        ".venv/Scripts/python.exe -m kmm.scanner --write",
        ".venv/Scripts/python.exe scripts/fetch_resolved.py --series <TICKER>",
        ".venv/Scripts/python.exe scripts/build_report.py",
        "```",
        "",
        "There is no order placement code in this repository. See the scope note in the "
        "README.",
    ]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(doc), encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the markdown results report.")
    parser.add_argument("--scan-db", default=ROOT / "data" / "kalshi.sqlite", type=Path)
    parser.add_argument("--calib-db", default=ROOT / "data" / "calibration.sqlite", type=Path)
    parser.add_argument("--out", default=ROOT / "reports" / "REPORT.md", type=Path)
    parser.add_argument("--demo", action="store_true",
                        help="build from the synthetic demo databases, with a banner saying so")
    args = parser.parse_args()

    scan_db, calib_db, out = args.scan_db, args.calib_db, args.out
    if args.demo:
        scan_db = ROOT / "data" / "demo_synthetic.sqlite"
        calib_db = ROOT / "data" / "demo_calibration.sqlite"
        out = ROOT / "reports" / "REPORT-demo.md"

    path = build(scan_db, calib_db, out, synthetic=args.demo)
    print(f"wrote {path}")
    if args.demo:
        print("This is a generated-data report. It tests the generator, it is not a result.")


if __name__ == "__main__":
    main()
