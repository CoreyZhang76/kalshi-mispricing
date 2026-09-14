"""Client-side token bucket that mirrors Kalshi's server-side limiter.

How Kalshi meters you (docs.kalshi.com/getting_started/rate_limits):

  - Every authenticated request costs tokens. Most cost 10.
  - Your account tier sets a refill RATE in tokens per second. Basic tier is
    200 read tokens/sec and 100 write tokens/sec, so Basic is about 20 reads
    per second.
  - Reads and writes are separate buckets, so heavy polling cannot starve order
    management (not that we place orders here).
  - The bucket holds a small reserve, so an idle client can burst above its
    steady rate. Basic read buckets hold one second of budget, higher tiers hold
    two.
  - Blowing the limit returns HTTP 429. There is no ban or cooldown, the bucket
    just keeps refilling, but a 429 still costs a round trip and leaves a hole
    in the data.

We keep our own bucket and wait locally rather than firing requests and catching
429s. The point is not politeness. A collector that trips the limiter produces a
dataset with irregular gaps, and irregular gaps are exactly the thing that
silently biases a "how long did this mispricing last" measurement: the gaps
cluster during busy periods, which is when mispricings actually happen.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

# Published per-second token budgets. Only the read column matters for a
# collector, but both are here so the numbers are visible to a reader.
TIERS = {
    "basic": {"read": 200, "write": 100},
    "advanced": {"read": 300, "write": 300},
    "expert": {"read": 600, "write": 600},
    "premier": {"read": 1000, "write": 1000},
    "paragon": {"read": 2000, "write": 2000},
    "prime": {"read": 4000, "write": 4000},
    "prestige": {"read": 10000, "write": 8000},
}

DEFAULT_REQUEST_COST = 10.0


@dataclass
class TokenBucket:
    """A token bucket limiter. `rate` tokens accrue per second up to `capacity`."""

    rate: float
    capacity: float
    tokens: float = field(default=0.0)
    _updated: float = field(default_factory=time.monotonic)
    _lock: asyncio.Lock | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # Start full. A fresh process can issue its first burst immediately,
        # which is what the server-side bucket also allows after idle time.
        self.tokens = self.capacity

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        self._updated = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

    async def acquire(self, cost: float = DEFAULT_REQUEST_COST) -> None:
        """Block until `cost` tokens are available, then spend them."""
        if cost > self.capacity:
            raise ValueError(
                f"cost {cost} exceeds bucket capacity {self.capacity}; it can never be satisfied"
            )
        # Created lazily so the bucket can be constructed outside a running loop.
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            while True:
                self._refill()
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
                deficit = cost - self.tokens
                await asyncio.sleep(deficit / self.rate)


def read_bucket(tier: str = "basic", safety: float = 0.8) -> TokenBucket:
    """Bucket for read endpoints.

    `safety` shaves the rate below the published limit. Our clock and Kalshi's
    are not the same clock, and network jitter can bunch requests together at
    the server even when we spaced them out here, so running at 100% of the
    published rate produces occasional 429s for no gain.
    """
    tier = tier.lower()
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}; expected one of {sorted(TIERS)}")
    rate = TIERS[tier]["read"] * safety
    seconds_of_burst = 1.0 if tier == "basic" else 2.0
    return TokenBucket(rate=rate, capacity=rate * seconds_of_burst)
