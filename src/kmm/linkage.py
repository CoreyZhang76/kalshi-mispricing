"""Infer which markets are logically linked, from Kalshi's own metadata.

The original plan was to hand-declare linked sets in a YAML file. Reading the API
spec changed that, because Kalshi publishes the structure directly and inferring
it is both less error-prone and less prone to me finding what I went looking for:

  - Each event carries `mutually_exclusive`. True means at most one market in the
    event resolves yes.
  - Each market carries `strike_type` (greater, greater_or_equal, less,
    less_or_equal, between, functional, custom, structured) plus `floor_strike`
    and `cap_strike`, the numeric bounds on the settlement value that produce a
    YES.

So a market is a claim about an interval of the real line, and the relations fall
out of set theory rather than from reading contract titles:

    greater(x)  -> YES on [x, inf)
    less(x)     -> YES on (-inf, x]
    between     -> YES on [floor, cap]

Interval containment gives implication. A disjoint cover of the whole line gives
an exhaustive partition. Nothing here depends on parsing English.

Two deliberate conservatisms:

  - A set is only marked exhaustive when its intervals are disjoint AND tile the
    line with no gaps. When that check is ambiguous we fall back to the weaker
    exclusive-only bound. Claiming exhaustiveness we do not have manufactures
    arbitrage that is not there, which is the expensive direction to be wrong in.
  - `functional`, `custom` and `structured` strike types are skipped entirely.
    Their settlement is defined by a formula or by prose, so we cannot establish
    the relation from metadata, and guessing would be worse than abstaining.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

NEG_INF = -math.inf
POS_INF = math.inf

# Strike types whose settlement region we can read off the metadata.
INTERVAL_STRIKE_TYPES = {"greater", "greater_or_equal", "less", "less_or_equal", "between"}


@dataclass
class Interval:
    """The settlement region of one market: YES if the outcome lands in here."""

    ticker: str
    low: float
    high: float
    strike_type: str

    def contains(self, other: "Interval") -> bool:
        """True if `other` is a subset of this interval, so other implies self."""
        return self.low <= other.low and other.high <= self.high

    def overlaps(self, other: "Interval") -> bool:
        return self.low < other.high and other.low < self.high

    def __str__(self) -> str:
        low = "-inf" if self.low == NEG_INF else f"{self.low:g}"
        high = "+inf" if self.high == POS_INF else f"{self.high:g}"
        return f"{self.ticker} [{low}, {high}]"


@dataclass
class InferredSet:
    """A group of markets with a no-arbitrage relation we can actually test."""

    name: str
    relation: str          # 'ladder' | 'partition'
    tickers: list[str]
    exhaustive: bool = False
    event_ticker: str = ""
    notes: str = ""

    def __str__(self) -> str:
        tag = "exhaustive" if self.exhaustive else "exclusive only"
        extra = f", {tag}" if self.relation == "partition" else ""
        return f"{self.name} [{self.relation}{extra}] {len(self.tickers)} markets: {self.notes}"


def market_interval(market: dict) -> Interval | None:
    """Turn one market's strike metadata into the interval it pays out on.

    Returns None when the strike type is not one we can reason about, or when the
    bound the type requires is missing.
    """
    strike_type = (market.get("strike_type") or "").lower()
    ticker = market.get("ticker", "?")
    if strike_type not in INTERVAL_STRIKE_TYPES:
        return None

    floor_strike = market.get("floor_strike")
    cap_strike = market.get("cap_strike")

    if strike_type in ("greater", "greater_or_equal"):
        if floor_strike is None:
            return None
        return Interval(ticker, float(floor_strike), POS_INF, strike_type)
    if strike_type in ("less", "less_or_equal"):
        if cap_strike is None:
            return None
        return Interval(ticker, NEG_INF, float(cap_strike), strike_type)
    # between
    if floor_strike is None or cap_strike is None:
        return None
    return Interval(ticker, float(floor_strike), float(cap_strike), strike_type)


def implication_pairs(intervals: list[Interval]) -> list[tuple[str, str]]:
    """All (narrower, wider) pairs where the first entails the second.

    Returned narrowest-first so the caller can feed them straight into
    relations.check_implication, whose argument order is (sub, sup).
    """
    pairs: list[tuple[str, str]] = []
    for a in intervals:
        for b in intervals:
            if a.ticker == b.ticker:
                continue
            if b.contains(a) and not a.contains(b):  # strict containment only
                pairs.append((a.ticker, b.ticker))
    return pairs


def tiles_the_line(intervals: list[Interval], tolerance: float | None = None) -> bool:
    """True if the intervals are disjoint and cover (-inf, +inf) with no gaps.

    The fiddly part is that Kalshi series use two different bucket conventions,
    and telling a convention apart from an actual missing bucket matters:

      shared edge:  [-inf, 2.9] [2.9, 3.0] [3.0, +inf]
      offset grid:  [-inf, 2.9] [3.0, 3.1] [3.2, +inf]

    The second leaves 2.95 formally uncovered, but the underlying number is only
    ever reported to one decimal, so it is exhaustive in practice. The first
    leaves nothing uncovered at all.

    So the convention is detected rather than assumed. If any two intervals share
    an exact edge, the series uses the shared-edge convention and we demand exact
    contiguity. If none do, it is an offset grid, and we allow neighbours to sit
    one grid step apart but no further. A genuinely missing bucket shows up as a
    two-step gap either way, and is rejected.
    """
    if not intervals:
        return False
    ordered = sorted(intervals, key=lambda i: (i.low, i.high))

    if ordered[0].low != NEG_INF or ordered[-1].high != POS_INF:
        return False  # open at one end, so some outcome is uncovered

    if tolerance is None:
        shares_edge = any(
            nxt.low == current.high for current, nxt in zip(ordered, ordered[1:])
        )
        if shares_edge:
            tolerance = 0.0
        else:
            # Infer the grid from the width of the finite buckets, NOT from the
            # gaps between them. Inferring it from the gaps is circular: with two
            # intervals there is exactly one gap, it defines itself as one grid
            # step, and every hole looks contiguous. A bucket's own width is
            # independent evidence of the grid the series is quoted on.
            widths = [i.high - i.low for i in ordered
                      if i.low != NEG_INF and i.high != POS_INF and i.high > i.low]
            # No finite bucket means no evidence, so demand exact contiguity.
            tolerance = min(widths) if widths else 0.0

    for current, nxt in zip(ordered, ordered[1:]):
        if current.high == POS_INF:
            return False  # an unbounded interval in the middle means overlap
        if nxt.low < current.high:
            return False  # overlap: two buckets could pay out together
        # Strike grids are decimal, so compare with a small slack to avoid a
        # float artefact like 0.30000000000000004 deciding the answer.
        if (nxt.low - current.high) - tolerance > 1e-9:
            return False  # a real gap, so some outcome is uncovered
    return True


def infer_sets(event: dict, markets: list[dict]) -> list[InferredSet]:
    """Work out which testable relations hold among one event's markets.

    `event` is an event object (needs `event_ticker` and `mutually_exclusive`);
    `markets` are its market objects. Returns zero, one or two sets: an event can
    be both a partition and contain implications, though in practice a mutually
    exclusive bucket set has no strict containments between its own members.
    """
    event_ticker = event.get("event_ticker", "?")
    intervals = [iv for iv in (market_interval(m) for m in markets) if iv is not None]
    skipped = len(markets) - len(intervals)
    out: list[InferredSet] = []

    if len(intervals) < 2:
        return out

    note_suffix = f" ({skipped} market(s) skipped: unreadable strike type)" if skipped else ""

    if event.get("mutually_exclusive"):
        # Sanity-check Kalshi's own flag against the geometry. If the intervals
        # overlap, two of them could resolve yes together and the upper bound
        # does not hold, whatever the flag says.
        overlapping = any(
            a.overlaps(b) for i, a in enumerate(intervals) for b in intervals[i + 1:]
        )
        if overlapping:
            out.append(
                InferredSet(
                    name=f"{event_ticker}-flagged-exclusive-but-overlapping",
                    relation="partition",
                    tickers=[iv.ticker for iv in intervals],
                    exhaustive=False,
                    event_ticker=event_ticker,
                    notes="SKIP: event flagged mutually_exclusive but strike intervals "
                          "overlap. Metadata and geometry disagree, so neither bound is "
                          "safe to trade on." + note_suffix,
                )
            )
            return out

        exhaustive = tiles_the_line(intervals)
        out.append(
            InferredSet(
                name=f"{event_ticker}-partition",
                relation="partition",
                tickers=[iv.ticker for iv in sorted(intervals, key=lambda i: i.low)],
                exhaustive=exhaustive,
                event_ticker=event_ticker,
                notes=("buckets tile the line, so YES prices must sum to exactly 100c"
                       if exhaustive else
                       "buckets are disjoint but leave outcomes uncovered, so only the "
                       "upper bound (sum <= 100c) is enforceable") + note_suffix,
            )
        )
        return out

    # Not exclusive: look for nesting instead.
    pairs = implication_pairs(intervals)
    if pairs:
        # Narrowest first, which for a "greater" ladder means highest strike first.
        # Ordering by interval WIDTH looks natural and is wrong here: every rung
        # of a "greater" ladder runs to +inf, so all the widths are infinite and
        # ties break arbitrarily. Rank by the containment relation itself instead.
        # The narrower an event, the more other events contain it.
        def containment_rank(iv: Interval) -> tuple:
            contained_by = sum(1 for other in intervals
                               if other.ticker != iv.ticker and other.contains(iv))
            return (-contained_by, -iv.low, iv.high)  # tiebreak deterministically

        ordered = sorted(intervals, key=containment_rank)
        out.append(
            InferredSet(
                name=f"{event_ticker}-ladder",
                relation="ladder",
                tickers=[iv.ticker for iv in ordered],
                exhaustive=False,
                event_ticker=event_ticker,
                notes=f"{len(pairs)} implication pair(s); ordered narrowest to widest"
                      + note_suffix,
            )
        )
    return out
