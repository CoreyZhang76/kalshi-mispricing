# kalshi-mispricing

Research tool for measuring cross-market pricing inefficiencies on Kalshi prediction
markets. It collects order book and trade data, checks no-arbitrage relationships
between economically linked contracts, and measures how well market prices are
calibrated against what actually happened.

**This is a measurement tool, not a trading system.** There is no order placement
code anywhere in this repo, and `kmm/rest.py` exposes read endpoints only. Any
profit figure it produces is a paper figure computed from observed quotes, which
is not the same thing as a realised one.

## Status

| Phase | What it does | State |
|---|---|---|
| 0 | API client, rate limiting, collector, storage | done |
| 1 | Identify linked market sets and their no-arbitrage rules | done |
| 2 | Mispricing scanner with fee-aware thresholds, duration tracking | done |
| 3 | Calibration and Brier score on resolved markets | done |
| 4 | Report generator and limitations | done; calibration on real data (no detectable miscalibration, 67 events); arbitrage collection too short for findings |

## Setup

The machine has no system Python, so everything runs through `uv`.

```bash
uv venv --python 3.12
uv pip install -e .
```

Create an API key at kalshi.com under Account then API Keys. Kalshi shows the
private key exactly once, so save it before closing the page. Then:

```bash
cp config/secrets.env.example config/secrets.env
```

Fill in `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`. Both `config/secrets.env`
and `*.pem` are gitignored.

Check everything works without touching the network:

```bash
.venv/Scripts/python.exe scripts/selftest.py
```

Then check the credentials:

```bash
.venv/Scripts/python.exe scripts/check_auth.py
```

## Use

Find markets worth tracking:

```bash
.venv/Scripts/python.exe scripts/discover_markets.py --candidates --min-volume 5000
```

See which no-arbitrage relations actually hold in a series, and get a config block
for them:

```bash
.venv/Scripts/python.exe scripts/discover_markets.py --series KXCPI --linkage --emit-yaml
```

Copy `config/markets.example.yaml` to `config/markets.yaml`, fill in the tickers,
then collect:

```bash
.venv/Scripts/python.exe -m kmm.collect --config config/markets.yaml
```

Ctrl-C to stop. Data lands in `data/kalshi.sqlite`, raw websocket messages in
`data/raw/`.

Build the report:

```bash
.venv/Scripts/python.exe scripts/build_report.py
```

Scan what was collected:

```bash
.venv/Scripts/python.exe -m kmm.scanner --db data/kalshi.sqlite --config config/markets.yaml
```

Build the calibration dataset from markets that already resolved. This does not
need the collector running, so it can produce a real result immediately:

```bash
.venv/Scripts/python.exe scripts/fetch_resolved.py --series KXHIGHNY --days 90
```

To see both analyses work without a Kalshi account, on generated data:

```bash
.venv/Scripts/python.exe scripts/demo_synthetic.py
.venv/Scripts/python.exe scripts/demo_calibration.py
```

## Layout

```
src/kmm/
  endpoints.py   base URLs for prod and demo
  auth.py        RSA-PSS request signing
  ratelimit.py   token bucket matching Kalshi's tiers
  prices.py      cent/dollar conversion and the YES/NO identity
  rest.py        read-only REST client (discovery, books, trades, candles)
  book.py        order book rebuilt from snapshot plus deltas
  ws.py          websocket collector with gap detection and reconnect
  storage.py     SQLite schema and writers
  collect.py     entrypoint: websocket feed plus a REST audit poll
  linkage.py     infers linked sets from strike metadata
  relations.py   the no-arbitrage bounds and their violations
  fees.py        Kalshi's quadratic fee, which sets the minimum real edge
  scanner.py     replays books, flags fee-clearing violations, times them
  calibration.py reliability curves, Brier decomposition, clustered bootstrap
  plots.py       reliability and horizon charts
scripts/
  selftest.py         offline tests, no credentials needed
  check_auth.py       credential and signature smoke test
  discover_markets.py browse the exchange to pick tickers
  demo_synthetic.py   runs the scanner on generated data, no key needed
  demo_calibration.py runs the calibration analysis on generated data
  fetch_resolved.py   builds the calibration dataset from settled markets
  build_report.py     generates reports/REPORT.md from whatever data exists
```

## Notes on the data

Two timestamps are stored on every row. `ts_ms` is Kalshi's, `recv_ms` is ours.
Latency claims have to use `ts_ms`, because `recv_ms` includes our own network
delay and would make the market look slower to correct itself than it is.

Websocket sequence gaps and disconnects are written to the `feed_gaps` table
rather than only logged. A delta stream with a hole in it produces a book that is
permanently wrong until the next snapshot, and a wrong book generates phantom
arbitrage. Any analysis window overlapping a gap needs excluding.

The collector also polls the full book over REST once a minute and compares the
top of book against its websocket reconstruction. Mismatches go to `feed_gaps`
with kind `rest_error`. That check exists to catch our own bugs, not Kalshi's.

## How linked sets are decided

Linked sets are inferred from Kalshi's metadata, not hand-declared. Events carry a
`mutually_exclusive` flag and markets carry `strike_type` with `floor_strike` and
`cap_strike`, so each market is an interval of the real line and the relations
follow from set theory rather than from reading contract titles.

This matters for the result, not just for convenience. If I hand-pick which markets
"should" be related, I am choosing the hypothesis after seeing the prices, and any
violation I find is partly an artefact of that choice.

Two places the code deliberately refuses to conclude anything:

- A set is only treated as an exhaustive partition when its intervals provably tile
  the whole line. `mutually_exclusive` means *at most* one market resolves yes, not
  *exactly* one. For an exclusive but non-exhaustive set, YES prices summing to less
  than $1 is correct pricing for "none of these happen", not an arbitrage, so only
  the upper bound is enforceable.
- `functional`, `custom` and `structured` strike types are skipped. Their settlement
  is defined by a formula or by prose, so the relation cannot be established from
  metadata and guessing would be worse than abstaining.

## Why the fee threshold decides everything

Kalshi charges `round_up_to_cent(0.07 * M * C * P * (1 - P))` per order, where M
is a per-series multiplier published on the series object. There is no settlement
fee, so a position held to resolution pays on entry only.

Two properties matter. It is quadratic and peaks at 50c, so the most uncertain
markets are the most expensive to trade. And it is charged per leg, so a
three-bucket partition priced around 33c each costs roughly 4.65c per contract to
enter, against a 1c tick.

The practical effect: a partition whose YES prices sum to 98c looks like a clean
2c arbitrage and is a 2.6c loss. Scanning against zero would produce a long list
of "mispricings" that are really just the cost of access. The scanner records
both the gross violation and the net-of-fees number, and only the latter counts
as a finding.

## Reading the calibration output

Three numbers matter more than the Brier score on its own.

**Excess reliability, not raw reliability.** Raw reliability never reaches zero on
real data. It averages the squared gap between price and observed frequency, and
that observed frequency is itself a finite sample, so noise alone contributes
about `(1/N) * sum over prices of p(1-p)`. With Kalshi's 99 possible prices and
3000 observations the floor is roughly 0.0055. Comparing raw reliability against
zero would call a perfectly calibrated market miscalibrated, so the code computes
the floor and reports the excess above it.

**Resolution, not just Brier.** Brier falls as resolution approaches on any
market, simply because the outcome becomes obvious. Resolution measures how far
the market moved from the base rate while still being right, so resolution rising
early is the evidence that it was anticipating rather than catching up.

**Events, not rows.** Ten snapshots of one contract are ten rows and one outcome,
and markets inside an event resolve together. Confidence intervals come from a
bootstrap over events; resampling rows would treat correlated observations as
independent and give an interval far too narrow to be honest.

**A fixed lead time, not a pooled price path.** Calibration is a property of a
forecast at a moment. A market quoted at 30c a week out and 70c an hour out can be
honest at both instants, but it has only one outcome, so no null hypothesis can
treat both quotes as simultaneously correct. The significance test therefore runs
on one observation per market, sliced at a fixed horizon. Pooling the path removed
all of the test's power on synthetic data: a heavy injected bias returned p=0.86
pooled and p=0.003 sliced.

**Significance comes from a simulated null, not a threshold.** Two earlier
approaches failed and both looked reasonable. A fixed cutoff on excess reliability
cannot work, because reliability is a squared error in probability units, so a
visible bias with gaps of 0.05 to 0.10 lands at a few thousandths. Bootstrapping
the observed data fails too, because resampling events with replacement changes
the number of observations behind each price group and breaks the noise-floor
correction: measured on synthetic data it sat about +0.007 above the point
estimate for honest and biased markets alike, flagging the honest one and missing
the biased one. What works is regenerating outcomes from the observed prices,
which is by construction a perfectly calibrated market with this dataset's exact
shape, and reading the observed value against that distribution.
