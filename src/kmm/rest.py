"""REST client for Kalshi market data.

REST and the WebSocket feed do different jobs, and a real collector uses both:

  REST is request/response and gives you a COMPLETE state on demand. It answers
  "what markets exist", "what are this market's rules and close time", "what is
  the full book right now", "did this market resolve yes or no". You cannot
  discover markets over the WebSocket, and you cannot get resolution outcomes
  from it either.

  The WebSocket pushes CHANGES. After one snapshot it sends only the price
  levels that moved, tagged with a sequence number. That is the only way to see
  the book as it actually evolved.

Polling REST instead of using the WebSocket looks simpler and is quietly wrong
for this project. Poll every second across 30 markets and you are at 30 requests
per second, which is above a Basic-tier budget, and you still only see the book
at one-second stroboscope intervals. A mispricing that opens and closes in 400ms
is invisible, and worse, it is invisible in a way that biases the result toward
"mispricings are rare and long-lived". The whole question we are asking is about
duration, so the measurement instrument has to be faster than the thing being
measured.

REST still earns its place here as the thing that bootstraps and audits:
discovery, metadata, resolution outcomes, and a periodic full-book poll that we
compare against the WebSocket-reconstructed book to catch reconstruction bugs.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterator
from urllib.parse import urlencode

import httpx

from . import endpoints
from .auth import KalshiSigner
from .ratelimit import TokenBucket, read_bucket


class KalshiRESTError(RuntimeError):
    def __init__(self, status: int, body: str, path: str):
        super().__init__(f"HTTP {status} from {path}: {body[:400]}")
        self.status = status
        self.body = body
        self.path = path


class KalshiREST:
    """Authenticated, rate-limited REST client. Read endpoints only.

    There are deliberately no order methods on this class. Adding one is the
    line between a research tool and a trading bot, and it is not crossed here.
    """

    def __init__(
        self,
        signer: KalshiSigner,
        env: str = "prod",
        bucket: TokenBucket | None = None,
        tier: str = "basic",
        timeout: float = 20.0,
    ):
        self.signer = signer
        self.env = env
        self.base = endpoints.rest_base(env)
        self.bucket = bucket or read_bucket(tier)
        self.client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> "KalshiREST":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None, retries: int = 3) -> dict:
        """GET a path relative to /trade-api/v2, e.g. "/markets".

        The signature covers the path WITHOUT the query string, so the params go
        on the URL but not into the signed message.
        """
        signed_path = endpoints.REST_PATH_PREFIX + path
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        url = self.base + path + ("?" + urlencode(clean) if clean else "")

        for attempt in range(retries + 1):
            await self.bucket.acquire()
            headers = self.signer.headers("GET", signed_path)
            resp = await self.client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            # 429 means our local bucket drifted from the server's. Back off and
            # retry: the server imposes no penalty, the bucket just refills.
            if resp.status_code == 429 and attempt < retries:
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            if resp.status_code >= 500 and attempt < retries:
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            raise KalshiRESTError(resp.status_code, resp.text, path)
        raise KalshiRESTError(0, "retries exhausted", path)

    async def paginate(self, path: str, key: str, params: dict[str, Any] | None = None,
                       max_pages: int = 200) -> Iterator[dict]:
        """Follow Kalshi's cursor pagination and yield items from `key`.

        Kalshi returns an empty-string cursor when there is nothing more. The
        max_pages guard exists because a mistaken filter can otherwise walk the
        entire exchange and burn the rate limit.
        """
        params = dict(params or {})
        out: list[dict] = []
        for _ in range(max_pages):
            page = await self.get(path, params)
            items = page.get(key) or []
            out.extend(items)
            cursor = page.get("cursor") or ""
            if not cursor or not items:
                break
            params["cursor"] = cursor
        return iter(out)

    # ---- discovery -------------------------------------------------------

    async def get_series_list(self, category: str | None = None) -> list[dict]:
        """Series are the top level: a recurring question family, e.g. CPI prints."""
        data = await self.get("/series", {"category": category})
        return data.get("series", [])

    async def get_events(self, series_ticker: str | None = None, status: str | None = None,
                         limit: int = 200) -> list[dict]:
        """An event is one instance of a series, e.g. "CPI release for May 2026".

        An event usually holds SEVERAL markets: the different strikes or
        thresholds. That grouping is where most of the cross-market structure we
        care about in Phase 1 comes from.
        """
        items = await self.paginate(
            "/events", "events",
            {"series_ticker": series_ticker, "status": status, "limit": limit, "with_nested_markets": "false"},
        )
        return list(items)

    async def get_event(self, event_ticker: str, with_nested_markets: bool = True) -> dict:
        """One event plus, optionally, its markets in a single call.

        Returns {"event": {...}, "markets": [...]}. The event object carries
        `mutually_exclusive`, and the markets carry `strike_type` and their
        strike bounds, which together are everything kmm/linkage.py needs to
        infer the no-arbitrage relations without reading any contract prose.
        """
        data = await self.get(
            f"/events/{event_ticker}",
            {"with_nested_markets": str(with_nested_markets).lower()},
        )
        event = data.get("event", data)
        # Kalshi returns the markets nested under the event when asked, but has
        # also returned them alongside it, so accept either shape.
        markets = data.get("markets") or event.get("markets") or []
        return {"event": event, "markets": markets}

    async def get_markets(self, event_ticker: str | None = None, series_ticker: str | None = None,
                          status: str | None = None, tickers: list[str] | None = None,
                          limit: int = 200, **extra) -> list[dict]:
        """A market is one binary contract. Status is one of unopened/open/closed/settled.

        Only one status filter may be supplied at a time, and some timestamp
        filters are mutually exclusive with it. See the endpoint docs.
        """
        params = {
            "event_ticker": event_ticker,
            "series_ticker": series_ticker,
            "status": status,
            "tickers": ",".join(tickers) if tickers else None,
            "limit": limit,
            **extra,
        }
        return list(await self.paginate("/markets", "markets", params))

    async def get_market(self, ticker: str) -> dict:
        data = await self.get(f"/markets/{ticker}")
        return data.get("market", data)

    # ---- live state ------------------------------------------------------

    async def get_orderbook(self, ticker: str, depth: int = 0) -> dict:
        """Full book for one market. depth=0 means all levels.

        Returns YES bids and NO bids only. There is no ask side in the response:
        a NO bid at 60c already is a YES offer at 40c. See kmm/prices.py.
        """
        data = await self.get(f"/markets/{ticker}/orderbook", {"depth": depth} if depth else None)
        return data.get("orderbook_fp") or data.get("orderbook") or {}

    async def get_trades(self, ticker: str | None = None, min_ts: int | None = None,
                         max_ts: int | None = None, limit: int = 1000) -> list[dict]:
        """Public tape. Useful for backfilling the period before the collector started."""
        params = {"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts, "limit": limit}
        return list(await self.paginate("/markets/trades", "trades", params))

    async def get_candlesticks(self, series_ticker: str, ticker: str, start_ts: int,
                               end_ts: int, period_interval: int = 1) -> list[dict]:
        """OHLC plus bid/ask history. period_interval is in minutes: 1, 60 or 1440.

        This is the backbone of Phase 3. For a market that already resolved, the
        1-minute candles give the implied probability path all the way to close,
        which is what a calibration curve needs and what we cannot get by having
        started the collector yesterday.
        """
        data = await self.get(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )
        return data.get("candlesticks", [])

    # ---- resolved / archived ---------------------------------------------

    async def get_historical_markets(self, series_ticker: str | None = None,
                                     event_ticker: str | None = None,
                                     tickers: list[str] | None = None, limit: int = 200) -> list[dict]:
        """Markets that settled before Kalshi's historical cutoff live in a
        separate archive and do not come back from /markets at all."""
        params = {
            "series_ticker": series_ticker,
            "event_ticker": event_ticker,
            "tickers": ",".join(tickers) if tickers else None,
            "limit": limit,
        }
        return list(await self.paginate("/historical/markets", "markets", params))

    async def get_exchange_status(self) -> dict:
        """Cheap call that proves credentials and connectivity work."""
        return await self.get("/exchange/status")

    async def get_endpoint_costs(self) -> dict:
        """Kalshi publishes the token cost of each endpoint for your account.

        Worth calling once and reading: most endpoints cost 10 tokens, but not
        all of them, and the rate limiter's assumptions are only as good as this.
        """
        return await self.get("/account/endpoint_costs")
