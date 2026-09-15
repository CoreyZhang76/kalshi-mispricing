"""Replay collected books, flag fee-clearing no-arbitrage violations, time them.

    python -m kmm.scanner --db data/kalshi.sqlite --config config/markets.yaml

A violation is not an instant, it is an interval. The question this project asks
is how long one survives, so the scanner does not just count detections: it
groups consecutive detections of the same package into an EPISODE that opens when
the violation first appears and closes when the prices stop violating the bound.
Counting raw detections instead would make a single 30-second mispricing on a
busy market look like hundreds of separate opportunities, purely because the book
updated often while it stood.

Three choices here are about not fooling ourselves:

**Fees are applied before anything is called a finding.** A violation smaller
than the cost of taking every leg is not an inefficiency, it is the price of
access. Both numbers are recorded, so the difference between "prices violated the
bound" and "violated it by enough to matter" stays visible rather than being
collapsed into one count.

**Episodes that never close are kept separate.** An episode still standing at the
end of the data did not last `end - start`, it lasted at least that long. Mixing
those into a mean duration biases it downward, and they are exactly the
interesting ones: a violation that never closes is either a genuine persistent
inefficiency or, more often, a market so illiquid that its quotes are stale.

**Episodes overlapping a feed gap are flagged, not silently dropped.** During a
sequence gap the reconstructed book was wrong, so any violation detected then may
be an artefact of our own missing data. Flagging rather than deleting means the
count of discarded episodes is itself reportable.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .config import CollectorConfig, LinkedSet
from .fees import FeeModel, net_edge_cents, round_trip_threshold_cents
from .linkage import InferredSet
from .relations import Quote, Violation, check_complement, check_ladder, check_partition


@dataclass
class Episode:
    """One continuous stretch during which a package violated its bound."""

    set_name: str
    relation: str
    tickers: tuple[str, ...]
    start_ms: int
    end_ms: int
    closed: bool = False
    peak_gross_edge_c: float = 0.0
    peak_net_c: float = 0.0
    peak_size: float = 0.0
    fee_at_peak_c: float = 0.0
    n_observations: int = 0
    spans_feed_gap: bool = False
    legs_at_peak: str = ""

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def clears_fees(self) -> bool:
        return self.peak_net_c > 0

    def __str__(self) -> str:
        state = "closed" if self.closed else "STILL OPEN at end of data"
        gap = " [SPANS FEED GAP]" if self.spans_feed_gap else ""
        return (
            f"{self.set_name} [{self.relation}] {self.duration_ms/1000:.1f}s {state}{gap}\n"
            f"    peak gross {self.peak_gross_edge_c:.1f}c/contract x{self.peak_size:g} "
            f"= {self.peak_gross_edge_c * self.peak_size:.0f}c, fees {self.fee_at_peak_c:.0f}c, "
            f"net {self.peak_net_c:+.0f}c\n"
            f"    {self.legs_at_peak}"
        )


def sets_from_config(cfg: CollectorConfig) -> list[InferredSet]:
    """Turn the YAML linked_sets block into scannable sets.

    The block is normally produced by `discover_markets.py --linkage --emit-yaml`,
    so the relations come from Kalshi's strike metadata rather than from a guess,
    and the YAML is a record of what was scanned rather than a hand-built
    hypothesis.
    """
    out: list[InferredSet] = []
    for s in cfg.linked_sets:
        out.append(
            InferredSet(
                name=s.name,
                relation=s.relation,
                tickers=list(s.tickers),
                exhaustive=bool(s.params.get("exhaustive", False)),
                notes=s.notes,
            )
        )
    return out


def evaluate(iset: InferredSet, quotes: dict[str, Quote]) -> list[Violation]:
    """Check one set against the current quotes. Missing legs mean no claim."""
    legs = [quotes.get(t) for t in iset.tickers]
    if any(q is None for q in legs):
        return []  # a set is only testable when every leg has a live quote
    legs = [q for q in legs if q is not None]

    if iset.relation == "ladder":
        return check_ladder(legs, set_name=iset.name)
    if iset.relation == "partition":
        return check_partition(legs, exhaustive=iset.exhaustive, set_name=iset.name)
    if iset.relation == "complement":
        found = [check_complement(q, set_name=iset.name) for q in legs]
        return [v for v in found if v is not None]
    return []


class Scanner:
    """Stateful replay: feed it quote updates in time order, get episodes out."""

    def __init__(self, sets: list[InferredSet], fee_model: FeeModel | None = None,
                 gap_windows: list[tuple[int, int]] | None = None):
        self.sets = sets
        self.fee_model = fee_model or FeeModel()
        self.gap_windows = gap_windows or []
        self.quotes: dict[str, Quote] = {}
        self.open: dict[tuple, Episode] = {}
        self.closed: list[Episode] = []
        # Which sets does each ticker participate in, so an update only
        # re-evaluates what it can actually affect.
        self.by_ticker: dict[str, list[InferredSet]] = {}
        for iset in sets:
            for ticker in iset.tickers:
                self.by_ticker.setdefault(ticker, []).append(iset)

    def update(self, quote: Quote, ts_ms: int) -> None:
        self.quotes[quote.ticker] = quote
        for iset in self.by_ticker.get(quote.ticker, []):
            found = evaluate(iset, self.quotes)
            seen_keys = set()
            for v in found:
                key = (iset.name, v.relation, tuple(leg.ticker for leg in v.legs))
                seen_keys.add(key)
                self._record(key, v, ts_ms)
            # Any package in this set that was violating and no longer is has closed.
            for key in [k for k in self.open if k[0] == iset.name and k not in seen_keys]:
                self._close(key, ts_ms)

    def _record(self, key: tuple, v: Violation, ts_ms: int) -> None:
        prices = [leg.price_c for leg in v.legs]
        size = v.max_size
        fees = round_trip_threshold_cents(prices, size, self.fee_model)
        net = net_edge_cents(v.edge_c, prices, size, self.fee_model)

        ep = self.open.get(key)
        if ep is None:
            ep = Episode(
                set_name=v.set_name, relation=v.relation,
                tickers=key[2], start_ms=ts_ms, end_ms=ts_ms,
            )
            self.open[key] = ep
        ep.end_ms = ts_ms
        ep.n_observations += 1
        # Track the peak by NET, not gross: the biggest tradable moment is the
        # one that matters, and gross can peak where fees eat more than it adds.
        if net > ep.peak_net_c or ep.n_observations == 1:
            ep.peak_net_c = net
            ep.peak_gross_edge_c = v.edge_c
            ep.peak_size = size
            ep.fee_at_peak_c = fees
            ep.legs_at_peak = "; ".join(str(leg) for leg in v.legs)

    def _close(self, key: tuple, ts_ms: int) -> None:
        ep = self.open.pop(key)
        ep.end_ms = ts_ms
        ep.closed = True
        ep.spans_feed_gap = self._overlaps_gap(ep)
        self.closed.append(ep)

    def _overlaps_gap(self, ep: Episode) -> bool:
        return any(ep.start_ms <= g_end and g_start <= ep.end_ms
                   for g_start, g_end in self.gap_windows)

    def finish(self) -> list[Episode]:
        """Close out the replay. Episodes still open are returned unclosed."""
        for key in list(self.open):
            ep = self.open.pop(key)
            ep.closed = False
            ep.spans_feed_gap = self._overlaps_gap(ep)
            self.closed.append(ep)
        return sorted(self.closed, key=lambda e: e.start_ms)


# ---------------------------------------------------------------------------
# Reading the collected data
# ---------------------------------------------------------------------------


def load_gap_windows(conn: sqlite3.Connection, pad_ms: int = 5000) -> list[tuple[int, int]]:
    """Time windows in which the reconstructed book may have been wrong.

    Padded on both sides, because a gap corrupts the book slightly before it is
    detected and until the next snapshot lands.
    """
    rows = conn.execute("SELECT detected_ms FROM feed_gaps").fetchall()
    return [(int(r[0]) - pad_ms, int(r[0]) + pad_ms) for r in rows]


def iter_quotes(conn: sqlite3.Connection, tickers: list[str]):
    """Yield (ts_ms, Quote) over the collected book, in exchange-time order.

    Prefers Kalshi's timestamp and falls back to ours when the feed did not send
    one. REST audit rows are excluded: they are a duplicate view of the same book
    taken a moment later, and replaying them would double count.
    """
    placeholders = ",".join("?" * len(tickers))
    sql = (
        "SELECT COALESCE(ts_ms, recv_ms) AS t, ticker, yes_bid_c, yes_bid_sz, yes_ask_c, yes_ask_sz "
        f"FROM book WHERE source != 'rest' AND ticker IN ({placeholders}) ORDER BY t ASC"
    )
    for t, ticker, yb, ybs, ya, yas in conn.execute(sql, tickers):
        yield int(t), Quote(
            ticker=ticker,
            yes_bid_c=yb, yes_bid_sz=ybs or 0.0,
            yes_ask_c=ya, yes_ask_sz=yas or 0.0,
            ts_ms=int(t),
        )


def scan_database(db_path: Path, sets: list[InferredSet],
                  fee_model: FeeModel | None = None) -> list[Episode]:
    conn = sqlite3.connect(db_path)
    try:
        tickers = sorted({t for s in sets for t in s.tickers})
        scanner = Scanner(sets, fee_model, gap_windows=load_gap_windows(conn))
        for ts_ms, quote in iter_quotes(conn, tickers):
            scanner.update(quote, ts_ms)
        return scanner.finish()
    finally:
        conn.close()


def write_episodes(db_path: Path, episodes: list[Episode]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS violation_episodes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                set_name        TEXT NOT NULL,
                relation        TEXT NOT NULL,
                tickers         TEXT NOT NULL,
                start_ms        INTEGER NOT NULL,
                end_ms          INTEGER NOT NULL,
                duration_ms     INTEGER NOT NULL,
                closed          INTEGER NOT NULL,
                clears_fees     INTEGER NOT NULL,
                peak_gross_c    REAL,
                peak_size       REAL,
                fee_at_peak_c   REAL,
                peak_net_c      REAL,
                n_observations  INTEGER,
                spans_feed_gap  INTEGER,
                legs_at_peak    TEXT
            );
            """
        )
        conn.execute("DELETE FROM violation_episodes")  # a rescan replaces prior results
        conn.executemany(
            "INSERT INTO violation_episodes (set_name, relation, tickers, start_ms, end_ms, "
            "duration_ms, closed, clears_fees, peak_gross_c, peak_size, fee_at_peak_c, "
            "peak_net_c, n_observations, spans_feed_gap, legs_at_peak) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (e.set_name, e.relation, json.dumps(list(e.tickers)), e.start_ms, e.end_ms,
                 e.duration_ms, int(e.closed), int(e.clears_fees), e.peak_gross_edge_c,
                 e.peak_size, e.fee_at_peak_c, e.peak_net_c, e.n_observations,
                 int(e.spans_feed_gap), e.legs_at_peak)
                for e in episodes
            ],
        )
        conn.commit()
    finally:
        conn.close()


def summarise(episodes: list[Episode]) -> str:
    """Headline counts, with the fee threshold and data quality kept visible."""
    if not episodes:
        return ("No no-arbitrage violations detected at all.\n"
                "That is a result, not a failure: it means the bounds held at every "
                "observed quote across the scanned markets.")

    clean = [e for e in episodes if not e.spans_feed_gap]
    real = [e for e in clean if e.clears_fees]
    closed_real = [e for e in real if e.closed]
    durations = sorted(e.duration_ms for e in closed_real)

    lines = [
        f"episodes detected:                 {len(episodes)}",
        f"  discarded, span a feed gap:      {len(episodes) - len(clean)}",
        f"  survive on clean data:           {len(clean)}",
        f"  still open at end of data:       {sum(1 for e in clean if not e.closed)}",
        f"  clear fees, so actually tradable: {len(real)}",
        f"    of those, still open:          {sum(1 for e in real if not e.closed)}",
    ]
    if durations:
        mid = durations[len(durations) // 2]
        lines += [
            "",
            f"closed fee-clearing episodes:      {len(closed_real)}",
            f"  median duration:                 {mid/1000:.1f}s",
            f"  shortest / longest:              {durations[0]/1000:.1f}s / {durations[-1]/1000:.1f}s",
            f"  largest net edge:                {max(e.peak_net_c for e in closed_real):.0f}c",
        ]
    if clean and not real:
        best = max(clean, key=lambda e: e.peak_gross_edge_c)
        lines += [
            "",
            "Every violation was smaller than the cost of trading it. The largest was",
            f"{best.peak_gross_edge_c:.1f}c per contract against {best.fee_at_peak_c:.0f}c of fees",
            f"on {best.peak_size:g} contracts. That is the fee floor doing its job, not a bug.",
        ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan collected books for no-arbitrage violations.")
    parser.add_argument("--db", default="data/kalshi.sqlite", type=Path)
    parser.add_argument("--config", default="config/markets.yaml", type=Path)
    parser.add_argument("--fee-multiplier", type=float, default=1.0,
                        help="series fee_multiplier; read it from the API rather than guessing")
    parser.add_argument("--show", type=int, default=10, help="how many episodes to print")
    parser.add_argument("--write", action="store_true", help="store episodes back into the database")
    args = parser.parse_args()

    cfg = CollectorConfig.from_yaml(args.config)
    sets = sets_from_config(cfg)
    if not sets:
        raise SystemExit(
            f"{args.config} declares no linked_sets. Generate a block with:\n"
            "  scripts/discover_markets.py --series <TICKER> --linkage --emit-yaml"
        )

    model = FeeModel(multiplier=args.fee_multiplier)
    episodes = scan_database(args.db, sets, model)

    print(summarise(episodes))
    ranked = sorted(episodes, key=lambda e: e.peak_net_c, reverse=True)[:args.show]
    if ranked:
        print(f"\n--- top {len(ranked)} by net edge ---")
        for e in ranked:
            print(e)

    if args.write:
        write_episodes(args.db, episodes)
        print(f"\nwrote {len(episodes)} episode(s) to {args.db}")


if __name__ == "__main__":
    main()
