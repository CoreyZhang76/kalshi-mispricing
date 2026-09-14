"""Price and size conversions, and the YES/NO identity that the book relies on.

Kalshi contracts pay $1 if the event happens and $0 if it does not. A price is
therefore a number between 0 and 1 dollar, quoted in whole cents, and it reads
directly as a probability: a YES contract at 37c is the market saying "37%",
before fees.

Every binary market has two sides, YES and NO, and they are the same bet from
opposite directions. Buying one YES and one NO of the same market costs you
whatever you paid and pays back exactly $1 at resolution regardless of outcome.
That identity is the reason the order book looks strange at first:

    Kalshi's book returns YES bids and NO bids only. There are no ask columns.

A resting NO bid at 60c IS a YES offer at 40c, for the same number of contracts,
because someone willing to pay 60c for NO is willing to sell YES at 40c. So:

    best_yes_ask = 100 - best_no_bid
    best_no_ask  = 100 - best_yes_bid

and the YES bid/ask spread is (best_yes_bid, 100 - best_no_bid).

The API now quotes prices as decimal-dollar strings ("0.3700") and sizes as
fixed-point strings with two decimals ("136.00"), so contract counts can be
fractional. Internally we store prices as integer cents, because the tick is one
cent and integers make the arbitrage comparisons exact, and sizes as floats.
"""

from __future__ import annotations

from typing import Iterable

# A Kalshi price level, as we store it: (price in whole cents, size in contracts).
Level = tuple[int, float]


def dollars_to_cents(value: str | float) -> int:
    """'0.3700' -> 37. Rounds to the nearest cent, which is the tick size."""
    return int(round(float(value) * 100))


def cents_to_dollars(cents: int) -> float:
    return cents / 100.0


def parse_size(value: str | float) -> float:
    """'136.00' -> 136.0. Sizes are fixed point with two decimals."""
    return float(value)


def parse_levels(raw: Iterable[Iterable[str]] | None) -> list[Level]:
    """Turn [['0.0800','300.00'], ...] into [(8, 300.0), ...], best price last.

    Kalshi returns levels ascending by price. We keep that order and let callers
    ask for the top of book explicitly, so there is no ambiguity about which end
    is "best" for a given side.
    """
    if not raw:
        return []
    out: list[Level] = []
    for level in raw:
        level = list(level)
        out.append((dollars_to_cents(level[0]), parse_size(level[1])))
    out.sort(key=lambda lv: lv[0])
    return out


def best_bid(levels: list[Level]) -> Level | None:
    """Highest price someone is bidding on this side, with its size."""
    return levels[-1] if levels else None


def yes_ask_from_no_bid(no_bid_cents: int) -> int:
    """A NO bid at p is a YES offer at 100 - p. See module docstring."""
    return 100 - no_bid_cents


def implied_probability(yes_bid: int | None, yes_ask: int | None) -> float | None:
    """Mid-price of the YES side, as a probability in [0, 1].

    The mid is a convenience, not a truth. On a market quoted 20 bid / 80 ask
    the mid says 50% while the book is telling you almost nothing. Anything that
    depends on this number should also carry the spread alongside it.
    """
    if yes_bid is None or yes_ask is None:
        return None
    return (yes_bid + yes_ask) / 200.0
