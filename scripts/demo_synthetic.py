"""Run the whole pipeline on generated data, with no Kalshi account required.

    .venv/Scripts/python.exe scripts/demo_synthetic.py

This exists so a reader can see the scanner work end to end, and so the pipeline
is exercised before any real data arrives. It is NOT a result. The prices here
were written by me, so the violations it finds are the violations I put in, and
nothing about the output says anything about Kalshi's actual efficiency.

What it does check, which is worth checking: that a fee-clearing violation is
found and timed correctly, that sub-fee noise is recorded but not counted as an
opportunity, and that an episode left standing at the end is reported as unclosed
rather than silently given a finite duration.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kmm.fees import FeeModel
from kmm.linkage import InferredSet
from kmm.scanner import scan_database, summarise
from kmm.storage import Store

DB = Path(__file__).resolve().parents[1] / "data" / "demo_synthetic.sqlite"
TICKERS = ["DEMO-B-LOW", "DEMO-B-MID", "DEMO-B-HIGH"]
START_MS = 1_760_000_000_000


def write_book_row(store: Store, ticker: str, ts_ms: int, bid: int, ask: int, size: float) -> None:
    """One top-of-book observation, in the same shape the live collector writes."""
    store.write_book(
        {
            "ticker": ticker, "ts_ms": ts_ms, "recv_ms": ts_ms + 12, "seq": None,
            "source": "ws_delta",
            "yes_bid_c": bid, "yes_bid_sz": size,
            "no_bid_c": 100 - ask, "no_bid_sz": size,
            "yes_ask_c": ask, "yes_ask_sz": size,
            "levels_json": "{}",
        }
    )


def build() -> None:
    if DB.exists():
        DB.unlink()
    store = Store(DB)
    store.start_run("demo", "synthetic", TICKERS, "demo", notes="generated, not real data")
    rng = random.Random(7)

    # Three buckets tiling the line. Fair value drifts slowly; quotes sit around
    # it with a 2c spread, so the asks normally sum to a bit over 100c, which is
    # what a real book looks like once the spread is included.
    fair = [30, 45, 25]
    t = START_MS

    for step in range(600):
        t += 1000
        # Small random walk that preserves the sum, so the set stays coherent.
        i, j = rng.sample(range(3), 2)
        if fair[i] > 5 and fair[j] < 95 and rng.random() < 0.3:
            fair[i] -= 1
            fair[j] += 1

        for idx, ticker in enumerate(TICKERS):
            bid, ask = fair[idx] - 1, fair[idx] + 1

            # A genuine, fee-clearing underround: every ask drops for 40 seconds,
            # pulling the sum to roughly 94c on decent size. The drop has to be
            # 3c per leg, not 2c: at 2c the edge is 4c/contract against about
            # 4.1c of fees, so it looks like an opportunity and is not one. That
            # margin is how thin the real threshold is.
            if 200 <= step < 240:
                ask -= 3
                bid -= 3
            # A sub-fee wobble: the sum dips to about 99c, a 1c edge that cannot
            # cover three legs of fees. This is the case that would inflate the
            # headline count if the scanner thresholded at zero.
            elif 400 <= step < 430 and idx == 0:
                ask -= 2
            # An underround that is still standing when the data ends.
            elif step >= 585:
                ask -= 2

            size = 50.0 if 200 <= step < 240 else rng.choice([5.0, 10.0, 20.0])
            write_book_row(store, ticker, t, bid, ask, size)

    store.end_run()
    store.close()


def main() -> None:
    build()
    sets = [InferredSet(name="DEMO-partition", relation="partition",
                        tickers=TICKERS, exhaustive=True,
                        notes="synthetic, buckets tile the line")]
    episodes = scan_database(DB, sets, FeeModel())

    print(f"database: {DB}")
    print("NOTE: generated data. The violations below are the ones this script "
          "wrote in, not findings about Kalshi.\n")
    print(summarise(episodes))

    ranked = sorted(episodes, key=lambda e: e.peak_net_c, reverse=True)[:5]
    print(f"\n--- top {len(ranked)} episodes by net edge ---")
    for e in ranked:
        print(e)


if __name__ == "__main__":
    main()
