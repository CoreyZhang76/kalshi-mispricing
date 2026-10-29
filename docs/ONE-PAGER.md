# Kalshi Cross-Market Mispricing Scanner

**Corey Zhang** | [github.com/CoreyZhang76/kalshi-mispricing](https://github.com/CoreyZhang76/kalshi-mispricing)
Personal project, October 2026. Status: pipeline complete through Phase 4. Calibration has a real result on 90 days of resolved KXHIGHNY markets (67 independent events): no miscalibration detectable at a 12-hour horizon. Live arbitrage collection has started but has run for under an hour, so there are no arbitrage findings yet.

---

## The question

Kalshi is a regulated prediction-market exchange where contracts pay $1 if an event
happens and $0 if it does not, so a price reads directly as a probability. Contracts
that reference the same underlying event are often listed separately, which means
their prices have to obey arithmetic relationships: a "CPI above 3.0%" contract
cannot be cheaper than "CPI above 3.1%", and a set of mutually exclusive outcomes
cannot have YES prices summing to more than $1.

This project asks two things:

1. **Do those relationships actually hold?** When they break, by how much, how
   often, and how long until the break closes.
2. **Are the prices well calibrated?** Of the contracts that traded at 70c, did
   roughly 70% of them resolve yes.

The second question is arguably the more interesting one. Arbitrage violations
mostly measure how fast the fastest participant is. Calibration measures whether
the crowd is actually any good at estimating probabilities.

## Scope, stated up front

This is a **data collection and analysis tool, not a trading system.** There is no
order placement code in the repo. Any profit figure it produces is computed from
observed quotes, which is not the same as a realised one: it ignores queue
position, slippage, and the fact that a quote you can see is not always a quote you
can hit.

## How it works

```
Kalshi WebSocket ──> order book reconstruction ──> SQLite ──> scanner ──> report
Kalshi REST ──────> discovery, metadata, outcomes ──┘         calibration
```

The WebSocket carries incremental book changes with sequence numbers, which is the
only way to see how the book actually evolved. REST handles what the socket cannot:
finding markets, reading resolution rules, and getting outcomes after settlement. It
also polls the full book once a minute so the socket-reconstructed book can be
checked against ground truth.

Three design decisions worth defending in an interview:

- **Both transports, not just REST polling.** Polling once a second across 30
  markets exceeds a Basic-tier rate limit and still only samples the book in
  stroboscope frames. A mispricing lasting 400ms would be invisible, and invisible
  in a direction that biases the result toward "mispricings are rare and
  long-lived". Since duration is the thing being measured, the instrument has to be
  faster than the phenomenon.
- **Feed gaps are data, not log lines.** A missed WebSocket message leaves the
  reconstructed book permanently wrong until the next snapshot, and a wrong book
  invents arbitrage that never existed. Gaps go into a `feed_gaps` table so any
  analysis window overlapping one can be excluded.
- **Two timestamps per row.** Kalshi's clock and ours. Latency claims use Kalshi's,
  because ours includes our own network delay and would make the market look slower
  to self-correct than it is.

## Built so far

| Module | Purpose |
|---|---|
| `auth.py` | RSA-PSS request signing against Kalshi's scheme |
| `ratelimit.py` | Client-side token bucket mirroring Kalshi's published tiers |
| `prices.py` | Cent conversion and the YES/NO identity the book depends on |
| `rest.py` | Read-only REST client: discovery, books, trades, candlesticks |
| `book.py` | Book rebuilt from snapshot plus deltas, with gap detection |
| `ws.py` | WebSocket collector with reconnect and raw message capture |
| `storage.py` | SQLite schema with run provenance and data-quality tracking |
| `linkage.py` | Infers linked sets from strike metadata (Phase 1) |
| `relations.py` | No-arbitrage bounds, checked on executable prices (Phase 1) |
| `fees.py` | Kalshi's quadratic fee, which sets the minimum real edge (Phase 2) |
| `scanner.py` | Replays books, flags fee-clearing violations, times them (Phase 2) |
| `calibration.py` | Reliability curves, Brier decomposition, clustered bootstrap (Phase 3) |
| `plots.py` | Reliability and time-to-resolution charts (Phase 3) |

Plus `scripts/build_report.py`, which generates the results document from whatever
data exists, choosing its headline sentences from the numbers rather than from a
template, and refusing to describe a synthetic run as a finding.

`scripts/selftest.py` runs offline with no credentials. It verifies signatures the
way Kalshi's server would, and walks a book through a snapshot and four deltas to
confirm that a NO bid at 56c correctly reads as a YES offer at 44c, that partial
lifts shrink a level, and that a full cancel makes the ask widen to the next level.

## Not done yet

**Arbitrage has no results yet.** The live collector is running on two linked ladders
(KXFED-26DEC and KXCPI-26OCT), but it has run for under an hour. The scanner has not
yet written to the database, because the collector holds it open. Weeks of collection
are needed before any frequency estimate means anything, and the scanner's
"no violations" line on that short run is not a finding.

**Calibration has a real result, on one series.** It covers 90 days of resolved
KXHIGHNY markets, 8,682 hourly observations across 357 markets and only 67 independent
events. At a 12-hour horizon the event-aware null test does not detect miscalibration
(p = 0.72). That is a null on a small sample, not evidence the market is well
calibrated, and it says nothing about other series.

## Limitations I already expect

- **Sample size.** A few weeks of collection on a handful of markets gives a small
  number of arbitrage events. Any frequency estimate will need an error bar wider
  than the estimate is likely to look.
- **Survivorship in market selection.** I choose which markets to track. If I pick
  illiquid ones because they look mispriced, I will find mispricings that are really
  just stale quotes.
- **Paper fills are not fills.** See scope above.
- **Calibration observations are not independent.** Ten snapshots of one contract
  are ten rows and one outcome, and markets inside an event resolve together. The
  row count overstates the sample by roughly an order of magnitude, so intervals
  come from a bootstrap over events and the event count is the number to quote.
- **Raw reliability has a noise floor.** It never reaches zero on finite data, so
  comparing it against zero would call an honest market miscalibrated. The code
  computes the floor and reports the excess above it.
- **Calibration needs more data than it looks.** Significance comes from a
  simulated null rather than a threshold, and on synthetic data the test needed
  roughly 1600 single-horizon observations to detect a substantial bias. A null
  result here will be as much a statement about sample size as about the market,
  and the report says so in those words.
- **Fees set the floor, and it is high.** Kalshi charges
  `round_up_to_cent(0.07 * M * C * P * (1 - P))` per leg, peaking at 50c. A
  three-leg partition around 33c each costs about 4.65c per contract to enter
  against a 1c tick, so a set summing to 98c looks like a 2c arbitrage and is a
  2.6c loss. The scanner thresholds on net-of-fees, not on zero.
- **Duration is measured in episodes, not detections.** A violation is an
  interval, and counting book updates while it stood would turn one 30-second
  mispricing into hundreds of "opportunities". Episodes still open when the data
  ends are reported separately, since they lasted *at least* that long.
