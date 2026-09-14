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
| 1 | Identify linked market sets and their no-arbitrage rules | not started |
| 2 | Mispricing scanner with fee-aware thresholds, duration tracking | not started |
| 3 | Calibration and Brier score on resolved markets | not started |
| 4 | Report and limitations | not started |

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

Copy `config/markets.example.yaml` to `config/markets.yaml`, fill in the tickers,
then collect:

```bash
.venv/Scripts/python.exe -m kmm.collect --config config/markets.yaml
```

Ctrl-C to stop. Data lands in `data/kalshi.sqlite`, raw websocket messages in
`data/raw/`.

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
scripts/
  selftest.py         offline tests, no credentials needed
  check_auth.py       credential and signature smoke test
  discover_markets.py browse the exchange to pick tickers
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
