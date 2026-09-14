"""No-arbitrage relations between linked markets, and the violations of them.

Everything here works in whole cents on the YES side, and everything is checked
against EXECUTABLE prices rather than mid-prices. That distinction is the whole
game. A mid-price is the average of a bid and an ask, and you cannot trade at it.
If you want to own a contract you pay the ask; if you want to be rid of one you
receive the bid. A scanner built on mids will report a stream of violations that
are really just the spread, and every one of them will be unexploitable.

Sign convention: `edge_c` is profit in cents per contract, before fees, on a
position held to resolution. Positive means the relation is violated in a way
you could in principle trade. Fees are applied in Phase 2, and they are what
decides whether a positive edge is an opportunity or just noise.

Three relations are implemented.

1. COMPLEMENT, within a single market. Buying one YES and one NO pays exactly $1
   no matter what happens, so the two asks cannot sum to less than $1.
2. IMPLICATION, between two markets where one event entails the other. If A
   happening guarantees B happening, then P(A) <= P(B) at all times.
3. PARTITION, across a set of mutually exclusive markets. If at most one can
   resolve yes, the YES prices cannot sum to more than $1. If additionally
   exactly one must resolve yes, they cannot sum to less than $1 either.

The asymmetry in (3) is worth dwelling on, because getting it wrong manufactures
fake arbitrage. Kalshi's event metadata has a `mutually_exclusive` flag, which
says at most one market resolves yes. It does NOT say at least one does. For a
set that is exclusive but not exhaustive, prices summing to less than $1 is
perfectly correct pricing: it is the market saying there is real probability that
none of them happen. Only the upper bound is enforceable. Exhaustiveness has to
be established separately, by checking that the strike buckets actually tile the
whole number line, which is what linkage.py does.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Quote:
    """Executable top of book for one market, YES side, in whole cents.

    `yes_ask_c` is derived from the best NO bid by the caller (see prices.py):
    Kalshi's book has no ask column. A side with no resting orders has None,
    which means that leg cannot be traded and any relation needing it is skipped
    rather than assumed.
    """

    ticker: str
    yes_bid_c: int | None = None
    yes_bid_sz: float = 0.0
    yes_ask_c: int | None = None
    yes_ask_sz: float = 0.0
    ts_ms: int | None = None

    @property
    def tradable_both_sides(self) -> bool:
        return self.yes_bid_c is not None and self.yes_ask_c is not None


@dataclass
class Leg:
    """One trade in an arbitrage package."""

    ticker: str
    action: str  # 'buy_yes' or 'sell_yes'
    price_c: int
    size: float

    def __str__(self) -> str:
        verb = "buy" if self.action == "buy_yes" else "sell"
        return f"{verb} {self.ticker} YES @ {self.price_c}c x{self.size:g}"


@dataclass
class Violation:
    """A no-arbitrage relation that does not hold at the observed quotes."""

    relation: str
    set_name: str
    edge_c: float           # profit in cents per contract, before fees
    max_size: float         # contracts available at the quoted prices
    legs: list[Leg] = field(default_factory=list)
    ts_ms: int | None = None
    detail: str = ""

    @property
    def gross_cents(self) -> float:
        """Total pre-fee profit if the whole available size is taken.

        Capped by the thinnest leg. An arbitrage is only as big as the smallest
        order standing in the way, and reporting edge without size is how a
        one-contract rounding artefact gets written up as a finding.
        """
        return self.edge_c * self.max_size

    def __str__(self) -> str:
        legs = "; ".join(str(leg) for leg in self.legs)
        return (
            f"[{self.relation}] {self.set_name}: edge={self.edge_c:.1f}c/contract "
            f"size={self.max_size:g} gross={self.gross_cents:.0f}c | {legs}"
        )


# ---------------------------------------------------------------------------
# 1. Complement, within one market
# ---------------------------------------------------------------------------


def check_complement(quote: Quote, set_name: str = "") -> Violation | None:
    """One YES plus one NO pays exactly $1, so their asks cannot sum below $1.

    Substituting yes_ask = 100 - no_bid and no_ask = 100 - yes_bid, the condition
    reduces to yes_bid > yes_ask: the YES side is bid above its own offer.

    On a correctly functioning exchange this NEVER fires, because the matching
    engine would have crossed those orders against each other. That is exactly
    why it is worth running. If it does fire, the overwhelmingly likely cause is
    a bug in our own book reconstruction, not free money on a regulated
    exchange, and we want to find that out here rather than in the results.
    """
    if not quote.tradable_both_sides:
        return None
    assert quote.yes_bid_c is not None and quote.yes_ask_c is not None
    edge = quote.yes_bid_c - quote.yes_ask_c
    if edge <= 0:
        return None
    return Violation(
        relation="complement",
        set_name=set_name or quote.ticker,
        edge_c=float(edge),
        max_size=min(quote.yes_bid_sz, quote.yes_ask_sz),
        legs=[
            Leg(quote.ticker, "buy_yes", quote.yes_ask_c, quote.yes_ask_sz),
            Leg(quote.ticker, "sell_yes", quote.yes_bid_c, quote.yes_bid_sz),
        ],
        ts_ms=quote.ts_ms,
        detail="book is crossed within a single market; suspect our reconstruction first",
    )


# ---------------------------------------------------------------------------
# 2. Implication, between two nested markets
# ---------------------------------------------------------------------------


def check_implication(sub: Quote, sup: Quote, set_name: str = "") -> Violation | None:
    """`sub` entails `sup`: whenever sub resolves yes, sup must resolve yes too.

    Example: "CPI above 3.1%" entails "CPI above 3.0%". You cannot have inflation
    above 3.1 without it being above 3.0, so P(above 3.1) <= P(above 3.0) must
    hold at every instant, whatever anyone believes about inflation.

    The trade when it breaks: buy the entailed contract, sell the entailing one.
    Settlement pays 1{sup} - 1{sub}, which is 0 or +1 and never negative, because
    sub cannot resolve yes while sup resolves no. So if you can put that position
    on for a credit, you are paid upfront to hold something that cannot lose.

        entry credit = bid(sub) - ask(sup)

    and the relation is violated exactly when that credit is positive, meaning
    the market you can SELL the narrower event at is higher than the price you
    can BUY the wider event at.

    Worth being clear about what "cannot lose" ignores: it assumes both contracts
    settle on the rule as written. Correlated resolution risk, an ambiguous
    source, or one market voiding while the other settles would break it, and
    that risk is not priced into `edge_c`.
    """
    if sub.yes_bid_c is None or sup.yes_ask_c is None:
        return None
    edge = sub.yes_bid_c - sup.yes_ask_c
    if edge <= 0:
        return None
    return Violation(
        relation="implication",
        set_name=set_name or f"{sub.ticker}=>{sup.ticker}",
        edge_c=float(edge),
        max_size=min(sub.yes_bid_sz, sup.yes_ask_sz),
        legs=[
            Leg(sup.ticker, "buy_yes", sup.yes_ask_c, sup.yes_ask_sz),
            Leg(sub.ticker, "sell_yes", sub.yes_bid_c, sub.yes_bid_sz),
        ],
        ts_ms=_latest(sub.ts_ms, sup.ts_ms),
        detail=f"P({sub.ticker}) <= P({sup.ticker}) required; sold narrower above wider",
    )


def check_ladder(ordered: list[Quote], set_name: str = "") -> list[Violation]:
    """Run check_implication across a chain ordered from narrowest to widest.

    For a "greater than" ladder that means highest strike first, since a higher
    threshold is the harder event and therefore entails every lower one.

    All pairs are checked, not just adjacent ones. A ladder can be locally
    monotone at every neighbouring step and still be violated between rungs that
    are further apart, and those wider-gap violations tend to be the larger ones.
    """
    out: list[Violation] = []
    for i, narrower in enumerate(ordered):
        for wider in ordered[i + 1:]:
            v = check_implication(narrower, wider, set_name=set_name or "ladder")
            if v is not None:
                v.detail += f" (rung {i} vs {ordered.index(wider)})"
                out.append(v)
    return out


# ---------------------------------------------------------------------------
# 3. Partition, across a set of mutually exclusive markets
# ---------------------------------------------------------------------------


def check_partition(quotes: list[Quote], exhaustive: bool, set_name: str = "") -> list[Violation]:
    """Bounds on a set where at most one market resolves yes.

    Upper bound, always available when the set is mutually exclusive:
        sum of YES prices <= $1
    If you can SELL every leg for a combined credit above $1, you collect that
    credit and pay out at most $1 at settlement, because at most one leg lands.

    Lower bound, only when the set is also exhaustive:
        sum of YES prices >= $1
    If you can BUY every leg for less than $1, you are guaranteed exactly $1 back,
    because exactly one leg lands.

    Applying the lower bound to a set that is exclusive but not exhaustive is a
    real and easy mistake. Prices summing to 92c across five buckets is not an
    8c arbitrage if there is a sixth outcome nobody listed: it is the market
    correctly pricing an 8% chance of "none of the above".
    """
    name = set_name or "partition"
    out: list[Violation] = []

    # Upper bound: sell every leg into its bid.
    if all(q.yes_bid_c is not None for q in quotes):
        total_bid = sum(q.yes_bid_c for q in quotes)  # type: ignore[misc]
        if total_bid > 100:
            out.append(
                Violation(
                    relation="partition_overround",
                    set_name=name,
                    edge_c=float(total_bid - 100),
                    max_size=min(q.yes_bid_sz for q in quotes),
                    legs=[Leg(q.ticker, "sell_yes", q.yes_bid_c, q.yes_bid_sz) for q in quotes],  # type: ignore[arg-type]
                    ts_ms=_latest(*(q.ts_ms for q in quotes)),
                    detail=f"YES bids sum to {total_bid}c across {len(quotes)} exclusive legs",
                )
            )

    # Lower bound: buy every leg at its ask. Requires exhaustiveness.
    if exhaustive and all(q.yes_ask_c is not None for q in quotes):
        total_ask = sum(q.yes_ask_c for q in quotes)  # type: ignore[misc]
        if total_ask < 100:
            out.append(
                Violation(
                    relation="partition_underround",
                    set_name=name,
                    edge_c=float(100 - total_ask),
                    max_size=min(q.yes_ask_sz for q in quotes),
                    legs=[Leg(q.ticker, "buy_yes", q.yes_ask_c, q.yes_ask_sz) for q in quotes],  # type: ignore[arg-type]
                    ts_ms=_latest(*(q.ts_ms for q in quotes)),
                    detail=f"YES asks sum to {total_ask}c across {len(quotes)} exhaustive legs",
                )
            )

    return out


def _latest(*values: int | None) -> int | None:
    """Timestamp a multi-leg violation by its most recent leg.

    A package is only observable once its last leg has updated, so the newest
    timestamp is when the opportunity became visible. Using the oldest would
    overstate how long it had been sitting there unexploited.
    """
    present = [v for v in values if v is not None]
    return max(present) if present else None
