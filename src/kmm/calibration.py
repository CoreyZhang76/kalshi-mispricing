"""Calibration: does a contract priced at 70c resolve yes about 70% of the time?

This is a different question from the arbitrage scan, and a more interesting one.
No-arbitrage violations measure INTERNAL CONSISTENCY: whether prices contradict
each other. A market can be perfectly consistent and still be completely wrong
about the world, and it can be badly calibrated without ever offering a risk-free
trade. Calibration measures CORRESPONDENCE: whether the numbers mean what they
claim to mean.

Three tools, in increasing order of what they tell you.

**Reliability curve.** Group every observation by its implied probability, then
plot the mean price in each bin against the fraction that actually resolved yes.
A well-calibrated market sits on the 45-degree diagonal. The classic deviation in
betting markets is the favourite-longshot bias: the curve sags below the diagonal
at the top (longshots priced too high, they win less often than their price says)
and rises above it at the bottom (favourites too cheap). If that shows up here it
is evidence of retail-driven mispricing, because it is a preference for lottery
tickets rather than an arithmetic error.

**Brier score.** The mean squared error of a probabilistic forecast,
(1/N) * sum (p_i - outcome_i)^2, where outcome is 1 or 0. Lower is better. It is
worth knowing the reference points: always forecasting the base rate scores
o*(1-o), always saying 50% scores 0.25, and a confident forecaster who is wrong
scores worse than the coward who says 50% every time. A raw Brier score on its
own is nearly uninterpretable, which is why the decomposition matters more.

**Murphy decomposition.** Brier = Reliability - Resolution + Uncertainty.
  - Reliability is calibration error. Zero is perfect, and it is the only term the
    market can be blamed for.
  - Resolution is how far the forecasts move away from the base rate while still
    being right. Higher is better. A market that says 50% every time has zero
    reliability error and zero resolution, and is useless.
  - Uncertainty is the base rate variance, o*(1-o). It is a property of the
    question set, not of the market, so comparing raw Brier scores across
    different market types is mostly comparing how hard their questions were.

Because Kalshi quotes in whole cents there are only 99 distinct forecast values,
so we can group by exact price and the decomposition is algebraically exact
rather than approximate.

The statistical trap in all of this is that observations are NOT independent, in
two separate ways, and ignoring either inflates the apparent sample size by an
order of magnitude. See `effective_sample_note` and the clustered bootstrap.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass


@dataclass
class Observation:
    """One price observation on a market whose outcome is now known."""

    ticker: str
    event_ticker: str          # the clustering unit: markets in an event resolve together
    ts_ms: int
    implied_prob: float        # in [0, 1]
    outcome: int               # 1 if it resolved yes, 0 if no
    hours_to_resolution: float

    @property
    def price_c(self) -> int:
        return int(round(self.implied_prob * 100))


@dataclass
class Bin:
    """One bucket of the reliability curve."""

    lo: float
    hi: float
    n: int
    mean_pred: float
    observed_freq: float
    ci_lo: float
    ci_hi: float

    @property
    def gap(self) -> float:
        """Observed minus predicted. Positive means the market was too cheap."""
        return self.observed_freq - self.mean_pred

    @property
    def significant(self) -> bool:
        """True when the prediction sits outside the observed frequency's interval.

        With few observations per bin the interval is wide and almost nothing is
        significant, which is the honest outcome rather than a disappointing one.
        """
        return not (self.ci_lo <= self.mean_pred <= self.ci_hi)


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Binomial confidence interval, Wilson rather than normal approximation.

    The normal approximation is badly wrong exactly where this analysis spends
    most of its time: near 0 and 1, where it produces intervals extending past
    the ends of the probability scale, and for small n, where it is too narrow.
    Since the interesting bins are the extreme ones (that is where the
    favourite-longshot bias lives) using the wrong interval would be a way to
    manufacture significance in precisely the bins the conclusion depends on.
    """
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def reliability_table(obs: list[Observation], n_bins: int = 10) -> list[Bin]:
    """Group observations into equal-width probability bins."""
    buckets: dict[int, list[Observation]] = defaultdict(list)
    for o in obs:
        idx = min(int(o.implied_prob * n_bins), n_bins - 1)
        buckets[idx].append(o)

    table: list[Bin] = []
    for idx in sorted(buckets):
        group = buckets[idx]
        n = len(group)
        successes = sum(o.outcome for o in group)
        # Rows are not independent: a market contributes many hourly snapshots and
        # the markets in one event resolve together. Taking the interval over rows
        # overstates precision badly, so it is taken over the bin's distinct events,
        # with the bin's observed frequency applied to that count. This is an
        # approximation, not an exact clustered interval; the event-aware test is
        # null_excess_test.
        n_events = len({o.event_ticker for o in group})
        events_yes = round(successes / n * n_events)
        lo, hi = wilson_interval(events_yes, n_events)
        table.append(
            Bin(
                lo=idx / n_bins, hi=(idx + 1) / n_bins, n=n,
                mean_pred=sum(o.implied_prob for o in group) / n,
                observed_freq=successes / n,
                ci_lo=lo, ci_hi=hi,
            )
        )
    return table


def brier_score(obs: list[Observation]) -> float:
    if not obs:
        return float("nan")
    return sum((o.implied_prob - o.outcome) ** 2 for o in obs) / len(obs)


@dataclass
class BrierParts:
    brier: float
    reliability: float   # calibration error, lower is better, 0 is perfect
    resolution: float    # discrimination, higher is better
    uncertainty: float   # base rate variance, a property of the questions
    base_rate: float
    n: int
    reliability_noise: float = 0.0  # what a PERFECT forecaster would score here

    @property
    def skill_vs_base_rate(self) -> float:
        """Fraction of the base-rate forecaster's error that the market removes.

        1.0 is perfect, 0.0 means the market is no better than always quoting the
        base rate, and negative means it is worse than that.
        """
        return (self.uncertainty - self.brier) / self.uncertainty if self.uncertainty else float("nan")

    @property
    def reliability_excess(self) -> float:
        """Calibration error above what finite samples produce on their own.

        This is the number to quote, not raw reliability. See
        `expected_reliability_noise` for why raw reliability is never zero.
        Values near zero mean "indistinguishable from perfectly calibrated at
        this sample size", which is a real and reportable finding.
        """
        return self.reliability - self.reliability_noise

    def __str__(self) -> str:
        return (
            f"n={self.n}  base rate={self.base_rate:.3f}\n"
            f"  Brier        {self.brier:.4f}   (lower is better; 0.25 = always saying 50%)\n"
            f"  reliability  {self.reliability:.4f}   (raw calibration error)\n"
            f"    noise floor {self.reliability_noise:.4f}   (what a PERFECT forecaster scores at this n)\n"
            f"    excess      {self.reliability_excess:+.4f}   (the real miscalibration; ~0 means none detectable)\n"
            f"  resolution   {self.resolution:.4f}   (discrimination; higher is better)\n"
            f"  uncertainty  {self.uncertainty:.4f}   (base rate variance; fixed by the questions)\n"
            f"  skill        {self.skill_vs_base_rate:+.3f}   (vs always forecasting the base rate)"
        )


def expected_reliability_noise(obs: list[Observation]) -> float:
    """The reliability a PERFECTLY calibrated market would still score here.

    Raw reliability is biased upward and never reaches zero on real data, which
    is a trap: it makes every market look miscalibrated if you only compare
    against 0.

    The reason is that reliability averages (f_k - o_k)^2 over price groups, and
    o_k is an observed frequency from a finite number of draws. Even when the
    true probability equals f_k exactly, o_k scatters around it with variance
    f_k(1 - f_k)/n_k. Substituting that into the reliability sum, the expected
    contribution of pure noise is

        (1/N) * sum_k n_k * f_k(1-f_k)/n_k  =  (1/N) * sum_k f_k(1-f_k)

    which depends only on how many distinct prices appeared, not on how much data
    sits behind each. With Kalshi's 99 possible prices and 3000 observations that
    floor is about 0.0055, so a market scoring 0.006 is essentially perfect while
    naively looking like it has measurable calibration error.

    Subtract this from raw reliability to get the part that is actually evidence.
    """
    n = len(obs)
    if n == 0:
        return float("nan")
    groups: dict[int, list[Observation]] = defaultdict(list)
    for o in obs:
        groups[o.price_c].append(o)
    total = 0.0
    for group in groups.values():
        f_k = sum(o.implied_prob for o in group) / len(group)
        total += f_k * (1 - f_k)
    return total / n


def brier_decomposition(obs: list[Observation]) -> BrierParts:
    """Murphy's decomposition: Brier = Reliability - Resolution + Uncertainty.

    Grouped by exact quoted price. Kalshi ticks in whole cents, so there are at
    most 99 distinct forecasts and the identity holds exactly. Grouping into wide
    bins instead would leave a within-bin variance term unaccounted for and the
    three parts would not sum back to the Brier score.
    """
    n = len(obs)
    if n == 0:
        return BrierParts(float("nan"), float("nan"), float("nan"), float("nan"), float("nan"), 0,
                          float("nan"))

    base_rate = sum(o.outcome for o in obs) / n
    groups: dict[int, list[Observation]] = defaultdict(list)
    for o in obs:
        groups[o.price_c].append(o)

    reliability = resolution = 0.0
    for group in groups.values():
        n_k = len(group)
        f_k = sum(o.implied_prob for o in group) / n_k
        o_k = sum(o.outcome for o in group) / n_k
        reliability += n_k * (f_k - o_k) ** 2
        resolution += n_k * (o_k - base_rate) ** 2
    reliability /= n
    resolution /= n

    return BrierParts(
        brier=brier_score(obs),
        reliability=reliability,
        resolution=resolution,
        uncertainty=base_rate * (1 - base_rate),
        base_rate=base_rate,
        n=n,
        reliability_noise=expected_reliability_noise(obs),
    )


# ---------------------------------------------------------------------------
# Dependence between observations
# ---------------------------------------------------------------------------


def effective_sample_note(obs: list[Observation]) -> str:
    """Describe how much less data there is here than the row count suggests.

    Two distinct problems, both of which shrink the real sample:

    1. Repeated sampling of one market. Ten snapshots of the same contract at
       different times are ten rows but ONE outcome. Treating them as ten
       independent observations divides the standard error by sqrt(10) for free.
    2. Markets within an event resolve together. In an exhaustive partition,
       exactly one leg resolves yes by construction, so the outcomes are
       mechanically dependent. The independent unit is the EVENT, not the market
       and certainly not the row.

    So the row count is an upper bound on sample size, the market count is closer,
    and the event count is the conservative one to quote.
    """
    rows = len(obs)
    markets = len({o.ticker for o in obs})
    events = len({o.event_ticker for o in obs})
    return (
        f"{rows} observations, but only {markets} distinct markets and {events} "
        f"independent events. Quote {events} as the sample size: rows repeat the same "
        f"outcome, and markets inside an event resolve together."
    )


@dataclass
class NullTest:
    """Result of testing observed miscalibration against a simulated null."""

    observed: float
    null_median: float
    null_95: float          # the 95th percentile of the null distribution
    p_value: float
    n_sims: int

    @property
    def detectable(self) -> bool:
        return self.p_value < 0.05

    def __str__(self) -> str:
        return (
            f"excess reliability {self.observed:+.4f} vs a perfectly calibrated null "
            f"of {self.null_median:+.4f}\n  (95th percentile {self.null_95:+.4f}), "
            f"p = {self.p_value:.3f} over {self.n_sims} simulations"
        )


def null_excess_test(obs: list[Observation], n_sims: int = 400, seed: int = 0) -> NullTest:
    """Could a perfectly calibrated market have produced this much miscalibration?

    Two approaches were tried before this one and both fail, which is worth
    recording because each looks reasonable:

      - Comparing excess reliability against a hand-picked constant. Reliability
        is a squared error in probability units, so a visible bias with gaps of
        0.05 to 0.10 lands at only a few thousandths and slips under any
        threshold lax enough to avoid false positives on small samples.
      - Bootstrapping the observed data by resampling events. Resampling with
        replacement duplicates events, which changes the number of observations
        behind each price group and therefore breaks the noise-floor correction.
        Measured on synthetic data the bootstrap sat about +0.007 above the point
        estimate for both an honest and a biased market, so the percentile
        interval flagged the honest one and the bias-corrected interval missed
        the biased one.

    What works is simulating the null directly. Keep the quoted prices exactly as
    observed, then regenerate outcomes treating each market's quoted price as the
    truth. That is by construction a perfectly calibrated market with this
    dataset's exact price distribution, sample sizes and repeat structure. Doing
    it many times gives the distribution of excess reliability under perfect
    calibration, and the observed value can be read against it.

    **Pass one observation per market**, sliced at a fixed time to resolution with
    `at_horizon`. Pooling a market's whole price path into this test destroys it,
    and the reason is worth understanding rather than just avoiding.

    Perfect calibration is a property of a forecast at a moment. A market quoted
    at 30c a week out and 70c an hour out can be perfectly calibrated at both
    instants, because it learned something in between. But it has only ONE
    outcome, so there is no way to simulate a null in which both quotes are
    simultaneously honest. Any null that draws a single outcome for the market
    has to pick one price to draw from, and every observation quoted away from
    that price then contributes miscalibration that the real data never had.

    Measured on synthetic data, pooling made the null noisier than the observed
    data and the test had no power at all: a heavy injected bias returned p=0.86.
    Sliced to a single horizon the same test returns p=0.003 on 1600 observations
    and p=0.77 on honest data.
    """
    if not obs:
        return NullTest(float("nan"), float("nan"), float("nan"), float("nan"), 0)

    observed = brier_decomposition(obs).reliability_excess

    by_ticker: dict[str, list[Observation]] = defaultdict(list)
    for o in obs:
        by_ticker[o.ticker].append(o)

    repeated = sum(1 for group in by_ticker.values() if len(group) > 1)
    if repeated:
        raise ValueError(
            f"{repeated} market(s) appear more than once. This test needs one "
            "observation per market: slice with at_horizon() first. See the "
            "docstring for why pooling a price path removes all of the test's power."
        )
    # Each market's own mean quoted price is the probability the null draws from.
    ticker_p = {t: sum(o.implied_prob for o in group) / len(group)
                for t, group in by_ticker.items()}

    rng = random.Random(seed)
    null_scores = []
    for _ in range(n_sims):
        simulated: list[Observation] = []
        for ticker, group in by_ticker.items():
            outcome = 1 if rng.random() < ticker_p[ticker] else 0
            simulated.extend(
                Observation(o.ticker, o.event_ticker, o.ts_ms, o.implied_prob,
                            outcome, o.hours_to_resolution)
                for o in group
            )
        null_scores.append(brier_decomposition(simulated).reliability_excess)

    null_scores.sort()
    at_least_as_extreme = sum(1 for s in null_scores if s >= observed)
    return NullTest(
        observed=observed,
        null_median=null_scores[len(null_scores) // 2],
        null_95=null_scores[int(0.95 * len(null_scores))],
        # The +1 convention keeps the p-value from ever being exactly zero, which
        # would claim more certainty than a finite number of simulations supports.
        p_value=(at_least_as_extreme + 1) / (n_sims + 1),
        n_sims=n_sims,
    )


def clustered_bootstrap_brier(obs: list[Observation], n_resamples: int = 1000,
                              seed: int = 0) -> tuple[float, float]:
    """95% interval for the Brier score, resampling EVENTS rather than rows.

    Resampling rows would treat correlated observations as independent and give
    an interval far too narrow to be honest. Resampling whole events preserves
    the dependence structure inside each one.
    """
    if not obs:
        return (float("nan"), float("nan"))
    by_event: dict[str, list[Observation]] = defaultdict(list)
    for o in obs:
        by_event[o.event_ticker].append(o)
    events = list(by_event.values())

    rng = random.Random(seed)
    scores = []
    for _ in range(n_resamples):
        sample: list[Observation] = []
        for _ in range(len(events)):
            sample.extend(rng.choice(events))
        scores.append(brier_score(sample))
    scores.sort()
    return (scores[int(0.025 * len(scores))], scores[int(0.975 * len(scores))])


# ---------------------------------------------------------------------------
# Does calibration improve as resolution approaches?
# ---------------------------------------------------------------------------

DEFAULT_HORIZONS = [(0, 1), (1, 6), (6, 24), (24, 72), (72, 168), (168, 10**6)]


def at_horizon(obs: list[Observation], hours: float) -> list[Observation]:
    """One observation per market: the one closest to `hours` before resolution.

    Calibration is a property of a forecast at a moment, so it has to be measured
    at a fixed lead time. This also makes the sample honest: one market, one
    outcome, one row, so the row count finally means what it appears to mean.
    """
    best: dict[str, Observation] = {}
    for o in obs:
        current = best.get(o.ticker)
        if current is None or abs(o.hours_to_resolution - hours) < abs(
            current.hours_to_resolution - hours
        ):
            best[o.ticker] = o
    return list(best.values())


def by_horizon(obs: list[Observation],
               horizons: list[tuple[float, float]] | None = None) -> list[tuple[str, BrierParts]]:
    """Brier decomposition split by time to resolution.

    The expected shape is that accuracy improves as the event approaches, simply
    because more is known. That is not evidence about market quality on its own:
    a market that got sharper only in the last hour learned nothing from anything
    except the outcome becoming obvious. The informative comparison is whether
    RESOLUTION rises early, which would mean the market was genuinely
    anticipating rather than just catching up.
    """
    horizons = horizons or DEFAULT_HORIZONS
    out = []
    for lo, hi in horizons:
        group = [o for o in obs if lo <= o.hours_to_resolution < hi]
        if group:
            label = f"{lo:g}-{hi:g}h" if hi < 10**6 else f">{lo:g}h"
            out.append((label, brier_decomposition(group)))
    return out


def summarise(obs: list[Observation]) -> str:
    """Text report suitable for dropping straight into the Phase 4 write-up."""
    if not obs:
        return "No resolved observations available."

    parts = brier_decomposition(obs)
    lo, hi = clustered_bootstrap_brier(obs)
    lines = [
        effective_sample_note(obs),
        "",
        str(parts),
        f"  Brier 95% CI  [{lo:.4f}, {hi:.4f}]  (bootstrap over events, not rows)",
        "",
        "reliability curve:",
        f"  {'bin':>10} {'n':>7} {'priced':>8} {'actual':>8} {'95% CI':>18} {'gap':>8}",
    ]
    for b in reliability_table(obs):
        flag = "  *" if b.significant else ""
        lines.append(
            f"  {b.lo:.1f}-{b.hi:.1f} {b.n:>9} {b.mean_pred:>8.3f} {b.observed_freq:>8.3f} "
            f"  [{b.ci_lo:.3f}, {b.ci_hi:.3f}] {b.gap:>+8.3f}{flag}"
        )
    lines.append("  * marks bins where the quoted price falls outside the observed interval")
    return "\n".join(lines)
