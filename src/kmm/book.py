"""In-memory order book, rebuilt from a snapshot plus a stream of deltas.

The WebSocket sends one `orderbook_snapshot` when you subscribe, then
`orderbook_delta` messages. A delta says "at price P on side S, the resting size
changed by D". Positive D means size was added, negative means it was removed or
filled. We apply them to a dict of price -> size and drop levels that reach zero.

Every message carries a `seq` number that increments by one per subscription. If
seq jumps, we missed a message and the book we are holding is now wrong: we have
no way to know what changed in the message we never saw, and the error is
permanent, because deltas are relative. The only correct response is to throw
the book away and get a fresh snapshot. Silently continuing is how a collector
ends up producing phantom arbitrage: a stale level that was actually cancelled
sits in your book at a crossed price forever, and the scanner dutifully reports
free money that never existed.

So gaps are treated as a hard reset AND recorded in the feed_gaps table, so that
any analysis window overlapping one can be excluded later.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .prices import Level


@dataclass
class OrderBook:
    """Aggregated price levels for one market.

    `yes` and `no` map price-in-cents to resting size. Both hold BIDS. There is
    no ask side to store: a NO bid at 60c is the YES offer at 40c.
    """

    ticker: str
    yes: dict[int, float] = field(default_factory=dict)
    no: dict[int, float] = field(default_factory=dict)
    last_seq: int | None = None
    last_ts_ms: int | None = None
    ready: bool = False  # False until the first snapshot lands

    def apply_snapshot(self, yes_levels: list[Level], no_levels: list[Level], seq: int | None,
                       ts_ms: int | None = None) -> None:
        self.yes = {p: s for p, s in yes_levels if s > 0}
        self.no = {p: s for p, s in no_levels if s > 0}
        self.last_seq = seq
        self.last_ts_ms = ts_ms
        self.ready = True

    def apply_delta(self, side: str, price_c: int, delta: float, seq: int | None,
                    ts_ms: int | None = None) -> None:
        side_map = self.yes if side == "yes" else self.no
        new_size = side_map.get(price_c, 0.0) + delta
        if new_size <= 1e-9:
            side_map.pop(price_c, None)
        else:
            side_map[price_c] = new_size
        self.last_seq = seq
        self.last_ts_ms = ts_ms

    # ---- top of book -----------------------------------------------------

    @property
    def best_yes_bid(self) -> Level | None:
        if not self.yes:
            return None
        p = max(self.yes)
        return (p, self.yes[p])

    @property
    def best_no_bid(self) -> Level | None:
        if not self.no:
            return None
        p = max(self.no)
        return (p, self.no[p])

    @property
    def best_yes_ask(self) -> Level | None:
        """Derived from the best NO bid. Size carries over unchanged, because the
        same resting order provides both readings."""
        nb = self.best_no_bid
        if nb is None:
            return None
        return (100 - nb[0], nb[1])

    @property
    def spread_c(self) -> int | None:
        bid, ask = self.best_yes_bid, self.best_yes_ask
        if bid is None or ask is None:
            return None
        return ask[0] - bid[0]

    def is_crossed(self) -> bool:
        """YES bid at or above YES ask. Within a single market this should never
        happen on a correct book: the exchange would have matched the orders. If
        we see it, the first suspect is our own reconstruction, not free money."""
        s = self.spread_c
        return s is not None and s <= 0

    def to_row(self, source: str, recv_ms: int, seq: int | None) -> dict:
        """Flatten to the shape storage.write_book expects."""
        import json

        yb = self.best_yes_bid
        nb = self.best_no_bid
        ya = self.best_yes_ask
        return {
            "ticker": self.ticker,
            "ts_ms": self.last_ts_ms,
            "recv_ms": recv_ms,
            "seq": seq,
            "source": source,
            "yes_bid_c": yb[0] if yb else None,
            "yes_bid_sz": yb[1] if yb else None,
            "no_bid_c": nb[0] if nb else None,
            "no_bid_sz": nb[1] if nb else None,
            "yes_ask_c": ya[0] if ya else None,
            "yes_ask_sz": ya[1] if ya else None,
            "levels_json": json.dumps(
                {
                    "yes": sorted(self.yes.items()),
                    "no": sorted(self.no.items()),
                },
                separators=(",", ":"),
            ),
        }


class SequenceTracker:
    """Checks that seq increments by exactly one per subscription id."""

    def __init__(self) -> None:
        self.expected: dict[int, int] = {}

    def check(self, sid: int, seq: int | None) -> tuple[bool, int | None]:
        """Returns (ok, expected_seq). ok=False means a gap was detected."""
        if seq is None:
            return True, None
        want = self.expected.get(sid)
        self.expected[sid] = seq + 1
        if want is None or seq == want:
            return True, want
        return False, want

    def reset(self, sid: int | None = None) -> None:
        if sid is None:
            self.expected.clear()
        else:
            self.expected.pop(sid, None)
