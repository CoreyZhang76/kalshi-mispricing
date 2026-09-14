# Kalshi Cross-Market Mispricing Scanner

**Corey Zhang** | [github.com/CoreyZhang76/kalshi-mispricing](https://github.com/CoreyZhang76/kalshi-mispricing)
Personal project, September 2026. Status: Phases 0 and 1 complete, no findings yet.

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

## Built so far (Phases 0 and 1)

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

`scripts/selftest.py` runs offline with no credentials. It verifies signatures the
way Kalshi's server would, and walks a book through a snapshot and four deltas to
confirm that a NO bid at 56c correctly reads as a YES offer at 44c, that partial
lifts shrink a level, and that a full cancel makes the ask widen to the next level.

## Not done yet

Phases 2 to 4: the fee-aware scanner, calibration and Brier scoring, and the
write-up. **No live data has been collected and there
are no results.** Everything above is validated against Kalshi's published OpenAPI
and AsyncAPI specifications plus offline tests, not against live traffic.

## Limitations I already expect

- **Sample size.** A few weeks of collection on a handful of markets gives a small
  number of arbitrage events. Any frequency estimate will need an error bar wider
  than the estimate is likely to look.
- **Survivorship in market selection.** I choose which markets to track. If I pick
  illiquid ones because they look mispriced, I will find mispricings that are really
  just stale quotes.
- **Paper fills are not fills.** See scope above.
- **Fees set the floor.** Kalshi's fee is a function of price, largest near 50c.
  A violation smaller than the round-trip fee is not an opportunity, it is noise,
  and the scanner has to threshold on that rather than on zero.
