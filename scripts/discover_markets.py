"""Browse the exchange and surface candidate market sets to track.

    .venv/Scripts/python.exe scripts/discover_markets.py --list-series
    .venv/Scripts/python.exe scripts/discover_markets.py --series KXCPI
    .venv/Scripts/python.exe scripts/discover_markets.py --candidates --min-volume 5000

Kalshi's hierarchy:

    series  a recurring question family      e.g. "monthly CPI print"
    event   one instance of that series      e.g. "CPI for May 2026"
    market  one binary contract in an event  e.g. "CPI above 3.1%"

An event that contains several markets is usually a ladder over the same
underlying number: "above 2.9", "above 3.0", "above 3.1". Those ladders are
where the tightest no-arbitrage relationships live, which is why --candidates
ranks events by market count and volume. This script only reports; choosing what
to track is a judgment call about liquidity and resolution horizon.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kmm.auth import KalshiSigner
from kmm.config import load_credentials
from kmm.rest import KalshiREST


def _fmt_cents(value) -> str:
    return "-" if value in (None, "") else f"{int(value):>3}c"


async def list_series(rest: KalshiREST, category: str | None) -> None:
    series = await rest.get_series_list(category)
    by_category: dict[str, list[dict]] = defaultdict(list)
    for s in series:
        by_category[s.get("category") or "(uncategorised)"].append(s)
    for cat in sorted(by_category):
        print(f"\n=== {cat} ({len(by_category[cat])} series) ===")
        for s in sorted(by_category[cat], key=lambda x: x.get("ticker", "")):
            print(f"  {s.get('ticker'):<24} {str(s.get('title'))[:70]}")


async def show_series(rest: KalshiREST, series_ticker: str) -> None:
    markets = await rest.get_markets(series_ticker=series_ticker, status="open")
    by_event: dict[str, list[dict]] = defaultdict(list)
    for m in markets:
        by_event[m.get("event_ticker") or "?"].append(m)

    print(f"{len(markets)} open markets across {len(by_event)} events in {series_ticker}\n")
    for event, group in sorted(by_event.items()):
        volume = sum(int(m.get("volume") or 0) for m in group)
        print(f"--- {event}   markets={len(group)}  event_volume={volume:,}")
        for m in sorted(group, key=lambda x: x.get("ticker", "")):
            print(
                f"    {m.get('ticker'):<34} "
                f"bid={_fmt_cents(m.get('yes_bid'))} ask={_fmt_cents(m.get('yes_ask'))} "
                f"vol={int(m.get('volume') or 0):>8,} oi={int(m.get('open_interest') or 0):>8,}  "
                f"{str(m.get('yes_sub_title') or m.get('subtitle') or '')[:34]}"
            )


async def candidates(rest: KalshiREST, min_volume: int, min_markets: int, top: int) -> None:
    """Rank multi-market events by liquidity.

    Volume matters more than it looks. A mispricing on a market that trades
    twice a day is not a mispricing, it is a stale quote, and the two are easy to
    confuse if you only look at the printed bid and ask.
    """
    markets = await rest.get_markets(status="open", limit=1000)
    by_event: dict[str, list[dict]] = defaultdict(list)
    for m in markets:
        by_event[m.get("event_ticker") or "?"].append(m)

    rows = []
    for event, group in by_event.items():
        if len(group) < min_markets:
            continue
        volume = sum(int(m.get("volume") or 0) for m in group)
        if volume < min_volume:
            continue
        quoted = [m for m in group if m.get("yes_bid") and m.get("yes_ask")]
        spreads = [int(m["yes_ask"]) - int(m["yes_bid"]) for m in quoted]
        rows.append(
            {
                "event": event,
                "series": group[0].get("series_ticker"),
                "n": len(group),
                "volume": volume,
                "median_spread": sorted(spreads)[len(spreads) // 2] if spreads else None,
                "title": str(group[0].get("title"))[:64],
            }
        )

    rows.sort(key=lambda r: r["volume"], reverse=True)
    print(f"{'event':<34}{'n':>4}{'volume':>12}{'med_spread':>12}  title")
    print("-" * 110)
    for r in rows[:top]:
        spread = "-" if r["median_spread"] is None else f"{r['median_spread']}c"
        print(f"{r['event']:<34}{r['n']:>4}{r['volume']:>12,}{spread:>12}  {r['title']}")
    print(f"\n{len(rows)} events matched. Inspect one with --series <SERIES_TICKER>.")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Explore Kalshi markets to decide what to track.")
    parser.add_argument("--list-series", action="store_true")
    parser.add_argument("--category", default=None)
    parser.add_argument("--series", default=None)
    parser.add_argument("--candidates", action="store_true")
    parser.add_argument("--min-volume", type=int, default=1000)
    parser.add_argument("--min-markets", type=int, default=2)
    parser.add_argument("--top", type=int, default=40)
    args = parser.parse_args()

    creds = load_credentials()
    signer = KalshiSigner.from_file(creds.key_id, creds.private_key_path)
    async with KalshiREST(signer, env=creds.env) as rest:
        if args.list_series:
            await list_series(rest, args.category)
        elif args.series:
            await show_series(rest, args.series)
        elif args.candidates:
            await candidates(rest, args.min_volume, args.min_markets, args.top)
        else:
            parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
