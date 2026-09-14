"""Smoke test: prove the key, the signature and the rate limiter all work.

    .venv/Scripts/python.exe scripts/check_auth.py

Run this before anything else. Most Kalshi integration problems are signature
problems, and they all surface here as a 401 rather than deep inside a collector.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kmm.auth import KalshiSigner
from kmm.config import load_credentials
from kmm.rest import KalshiREST, KalshiRESTError


async def main() -> int:
    creds = load_credentials()
    print(f"env={creds.env}  key_id={creds.key_id[:8]}...  key_file={creds.private_key_path}")

    signer = KalshiSigner.from_file(creds.key_id, creds.private_key_path)
    async with KalshiREST(signer, env=creds.env) as rest:
        try:
            status = await rest.get_exchange_status()
        except KalshiRESTError as exc:
            print(f"FAILED: {exc}")
            if exc.status == 401:
                print(
                    "\n401 means the signature did not verify. The usual causes, in order:\n"
                    "  - the system clock is off by more than a few seconds\n"
                    "  - the timestamp was sent in seconds rather than milliseconds\n"
                    "  - the signed path was missing the /trade-api/v2 prefix\n"
                    "  - the key belongs to the other environment (prod key against demo, or vice versa)"
                )
            return 1
        print(f"exchange status: {json.dumps(status)}")

        # One real data call, to confirm read endpoints work and not just /status.
        markets = await rest.get_markets(status="open", limit=5)
        print(f"sample open markets ({len(markets)}):")
        for m in markets[:5]:
            print(f"  {m.get('ticker'):<32} {str(m.get('title'))[:60]}")

        try:
            costs = await rest.get_endpoint_costs()
            print(f"endpoint token costs: {json.dumps(costs)[:400]}")
        except KalshiRESTError as exc:
            print(f"(endpoint costs unavailable: {exc.status})")

    print("\nOK: auth, signing and a read endpoint all work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
