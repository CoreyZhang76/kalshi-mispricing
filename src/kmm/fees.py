"""Kalshi's trading fee, and why it sets the floor on what counts as a finding.

The fee on a filled order is

    fee = round_up_to_cent( 0.07 * M * C * P * (1 - P) )

where C is the contract count, P is the price in dollars, and M is a per-series
multiplier that Kalshi publishes on the series object (`fee_multiplier`, paired
with `fee_type`). There is no settlement fee: if your contract resolves, you get
the full $1 with nothing deducted.

Three consequences that matter for this project.

**It is quadratic, and worst in the middle.** P*(1-P) peaks at P=0.5, so a
contract at 50c costs 1.75c per contract to trade while one at 5c costs 0.33c.
The most uncertain markets, which are exactly the ones where a mispricing is most
plausible, are the most expensive to trade.

**It is symmetric between YES and NO.** Selling YES at 45c is really buying NO at
55c, and 0.45*0.55 is the same as 0.55*0.45. So a leg costs the same whichever
direction you take it, and the fee can be computed from the YES price alone
without tracking which side of the trade we are modelling.

**It is per leg, so multi-leg arbitrage is expensive.** A three-bucket partition
priced around 33c each costs roughly 1.55c per contract per leg to enter, so
about 4.6c in total. The tick size is 1c. That means the YES prices have to sum
to something like 95c or less before an underround is worth anything at all, and
a set summing to 98c, which looks like a clean 2c edge, is a 2.6c loss.

This is the single most important threshold in the project. Scanning against zero
would report a large number of "mispricings", almost all of them smaller than the
cost of trading them. Those are not inefficiencies, they are the price of access.
Reporting them as findings would be the main way this analysis could be wrong in
an embarrassing and entirely self-inflicted direction.

The rounding is per order rather than per contract, which quietly penalises small
size: one contract at 50c pays a whole cent on a theoretical 1.75c/100 fee. So
tiny arbitrages are worse than the linear math suggests, and the code applies the
rounding at the actual size rather than scaling a per-contract rate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# The constant in Kalshi's General Trading Fees table. Quoted in the published
# fee schedule at kalshi.com/docs/kalshi-fee-schedule.pdf.
BASE_FEE_RATE = 0.07

# Maker fees, where a series has them, are a fraction of the taker fee.
MAKER_FRACTION = {
    "quadratic": 0.0,                           # no maker fee at all
    "quadratic_with_maker_fees": 0.25,
    "quadratic_with_combo_maker_fees": 0.5,
    "flat": 0.0,                                # separate schedule, not modelled
}


def _ceil_cents(fee_dollars: float) -> float:
    """Dollars to whole cents, rounded up, without the float artefacts.

    The naive math.ceil(dollars * 100) is wrong here often enough to matter:
    0.07 * 100 * 0.5 * 0.5 evaluates to 1.7500000000000002, so a fee that should
    be exactly 175c ceils to 176c. Every fee sitting exactly on a cent boundary
    would be overstated by a cent, which biases the scanner toward rejecting
    genuine violations. Snapping to nine decimals first removes the artefact
    while staying far finer than the $0.000001 Kalshi itself rounds to.
    """
    return math.ceil(round(fee_dollars * 100.0, 9))


@dataclass
class FeeModel:
    """Fee parameters for one series.

    Defaults match the standard quadratic schedule with no multiplier. Real runs
    should read `fee_type` and `fee_multiplier` off the series object rather than
    trusting these, because Kalshi changes them per series and schedules changes
    in advance (there are `/series/fee_changes` and `/events/fee_changes`
    endpoints for exactly that reason).
    """

    multiplier: float = 1.0
    fee_type: str = "quadratic"
    verified_against_fills: bool = False  # set true only after checking real fills

    def taker_fee_cents(self, count: float, price_c: int) -> float:
        """Fee in cents for taking `count` contracts at `price_c` cents.

        Rounded up to a whole cent on the order, which is how Kalshi charges it.
        """
        if count <= 0:
            return 0.0
        if self.fee_type == "flat":
            raise NotImplementedError(
                "This series uses Kalshi's flat Specific Trading Fees table, which is a "
                "separate schedule. Read it before scanning this series rather than "
                "applying the quadratic formula and getting a wrong threshold."
            )
        price = price_c / 100.0
        fee_dollars = BASE_FEE_RATE * self.multiplier * count * price * (1.0 - price)
        return _ceil_cents(fee_dollars)

    def maker_fee_cents(self, count: float, price_c: int) -> float:
        """Fee if the leg rested and was filled rather than crossing the spread.

        Not used by the scanner. Resting an order is how you would actually cut
        the fee bill, but it forfeits the certainty of the fill, and queue
        position is exactly the thing this dataset cannot model. Included so the
        gap between the paper number and a realistic one stays visible.
        """
        fraction = MAKER_FRACTION.get(self.fee_type, 0.0)
        if fraction == 0.0:
            return 0.0
        price = price_c / 100.0
        fee_dollars = BASE_FEE_RATE * self.multiplier * fraction * count * price * (1.0 - price)
        return _ceil_cents(fee_dollars)

    @classmethod
    def from_series(cls, series: dict) -> "FeeModel":
        """Build from a Kalshi series object."""
        return cls(
            multiplier=float(series.get("fee_multiplier") or 1.0),
            fee_type=(series.get("fee_type") or "quadratic").lower(),
        )


def round_trip_threshold_cents(prices_c: list[int], count: float = 1.0,
                               model: FeeModel | None = None) -> float:
    """Total entry fee, in cents, for taking every leg of a package.

    This is the minimum gross edge a violation needs before it is worth anything.
    Only entry is charged: these positions are held to resolution, and settlement
    is free, so there is no exit fee to pay.
    """
    model = model or FeeModel()
    return sum(model.taker_fee_cents(count, p) for p in prices_c)


def net_edge_cents(gross_edge_c: float, prices_c: list[int], count: float,
                   model: FeeModel | None = None) -> float:
    """Profit in cents after fees for taking `count` of every leg.

    `gross_edge_c` is per contract and pre-fee, as produced by kmm.relations.
    """
    fees = round_trip_threshold_cents(prices_c, count, model)
    return gross_edge_c * count - fees
