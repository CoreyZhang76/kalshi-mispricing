# Kalshi cross-market mispricing: results

Generated 2026-09-16 16:23 UTC by `scripts/build_report.py`.

> **This report was built from generated data.** The numbers below are a test of
> the report generator, not findings about Kalshi. Every violation and every bias
> shown here was written in by `scripts/demo_synthetic.py` and
> `scripts/demo_calibration.py`. Nothing in this document should be quoted as a
> result.


## What was measured

Two separate questions, deliberately kept apart.

1. **Internal consistency.** Do prices on logically linked contracts contradict each other, and
   if so by how much and for how long? Linked sets are inferred from Kalshi's strike metadata,
   not hand-picked, so the hypothesis is not chosen after seeing the prices.
2. **Correspondence with reality.** Do contracts priced at 70c resolve yes about 70% of the time?
   A market can be perfectly self-consistent and still wrong about the world.

Collection: 1 collector run(s) spanning 0.0 hours, 0 feed gap(s) or disconnect(s) recorded.

### Limitations

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


## Findings

### Arbitrage

- **2** violation episodes detected in total.
- **0** discarded for overlapping a feed gap, where our reconstructed book may have been wrong.
- **1** of the remaining 2 cleared Kalshi's fees and was therefore tradable in principle.

Of the 1 tradable episode, **1** closed within the data and **0** were still standing when collection ended.

- Median duration: **40.0s**
- Range: 40.0s to 40.0s
- Largest net edge: **148c** after fees

**Headline: 1 tradable violation was found.** Whether that represents
exploitable inefficiency is a separate question from whether it represents a
detectable one: see the limitations on queue position above.


### Calibration

**Headline: no miscalibration is detectable at this sample size.** Measured 12 hours
before resolution on 480 markets, excess reliability is +0.0077 against a
perfectly calibrated null whose 95th percentile is +0.0094 (p = 0.100).

That is not the same as proving the market is well calibrated. It means this much data
cannot tell the difference. On synthetic data this test needed roughly 1600 single-horizon
observations to detect a substantial bias, so a null here is as much a statement about the
sample size as about the market.

![Reliability curve](figures/report_reliability_demo.png)

| metric | value | reading |
|---|---|---|
| Brier | 0.1924 | lower is better; 0.25 is always saying 50% |
| Brier 95% CI | [0.1804, 0.2051] | bootstrap over events, not rows |
| excess reliability at 12h | +0.0077 | one observation per market |
| null 95th percentile | +0.0094 | what perfect calibration produces here |
| **p-value** | **0.100** | **not distinguishable from calibrated** |
| raw reliability | 0.0106 | calibration error before debiasing |
| noise floor | 0.0058 | what a perfect forecaster scores here |
| **excess reliability** | **+0.0049** | **the real miscalibration** |
| resolution | 0.0675 | discrimination; higher is better |
| uncertainty | 0.2493 | base rate variance; fixed by the questions |
| skill | +0.228 | versus always forecasting the base rate |

Sample: 2880 observations, but only 480 distinct markets and 120 independent events. Quote 120 as the sample size: rows repeat the same outcome, and markets inside an event resolve together.

#### Bins

| bin | n | mean price | resolved yes | 95% CI | gap |
|---|---|---|---|---|---|
| 0.0 to 0.1 | 70 | 0.036 | 0.114 | [0.059, 0.210] | +0.078 \* |
| 0.1 to 0.2 | 155 | 0.161 | 0.181 | [0.128, 0.249] | +0.020 |
| 0.2 to 0.3 | 296 | 0.244 | 0.206 | [0.164, 0.256] | -0.038 |
| 0.3 to 0.4 | 399 | 0.345 | 0.318 | [0.275, 0.366] | -0.027 |
| 0.4 to 0.5 | 479 | 0.446 | 0.388 | [0.346, 0.433] | -0.057 \* |
| 0.5 to 0.6 | 409 | 0.545 | 0.597 | [0.548, 0.643] | +0.051 \* |
| 0.6 to 0.7 | 435 | 0.648 | 0.747 | [0.704, 0.786] | +0.099 \* |
| 0.7 to 0.8 | 369 | 0.744 | 0.832 | [0.790, 0.867] | +0.088 \* |
| 0.8 to 0.9 | 173 | 0.833 | 0.879 | [0.822, 0.919] | +0.046 |
| 0.9 to 1.0 | 95 | 0.958 | 0.842 | [0.756, 0.902] | -0.116 \* |

Brier 0.1924, excess reliability +0.0049, resolution 0.0675, n=2880.

#### Does it sharpen as resolution approaches?

![Score by horizon](figures/report_horizon_demo.png)

Brier falling toward resolution says little on its own, since the outcome becomes obvious.
The informative line is resolution: if it rises early, the market was anticipating rather
than catching up.

| horizon | n | Brier | excess reliability | resolution |
|---|---|---|---|---|
| 0-1h | 480 | 0.1761 | +0.0031 | 0.1059 |
| 1-6h | 480 | 0.1765 | +0.0018 | 0.1045 |
| 6-24h | 480 | 0.1780 | +0.0077 | 0.1105 |
| 24-72h | 480 | 0.2080 | +0.0067 | 0.0823 |
| 72-168h | 480 | 0.2029 | +0.0036 | 0.0842 |
| >168h | 480 | 0.2129 | +0.0127 | 0.0834 |

## Reproducing this

```bash
.venv/Scripts/python.exe scripts/selftest.py          # offline, no credentials
.venv/Scripts/python.exe scripts/check_auth.py        # verify API credentials
.venv/Scripts/python.exe -m kmm.collect --config config/markets.yaml
.venv/Scripts/python.exe -m kmm.scanner --write
.venv/Scripts/python.exe scripts/fetch_resolved.py --series <TICKER>
.venv/Scripts/python.exe scripts/build_report.py
```

There is no order placement code in this repository. See the scope note in the README.