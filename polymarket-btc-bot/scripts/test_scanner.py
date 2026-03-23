from __future__ import annotations

import aiohttp
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_settings  # noqa: E402
from src.markets.scanner import MarketScanner  # noqa: E402


async def main() -> int:
    scanner = MarketScanner(get_settings())
    async with aiohttp.ClientSession() as session:
        scanner._session = session  # intentional for one-off verification
        await scanner._scan()

    markets = list(scanner.active_markets.values())
    if not markets:
        print(
            "No active 5-min markets right now — try during high volume hours (12:00–22:00 UTC)"
        )
        return 0

    assets_seen = set()
    now = datetime.now(timezone.utc)

    for m in sorted(markets, key=lambda x: x.end_time):
        assets_seen.add(m.asset)
        remaining = int((m.end_time - now).total_seconds())
        end_str = m.end_time.astimezone(timezone.utc).strftime("%H:%M:%S UTC")
        print(f'Found market: "{m.question}"')
        print(f"  market_id:  {m.market_id}")
        print(f"  yes_token:  {m.yes_token_id}")
        print(f"  no_token:   {m.no_token_id}")
        print(f"  end_time:   {end_str} ({remaining}s remaining)")
        print(f"  yes_price:  ${m.current_yes_price}")
        print(f"  no_price:   ${m.current_no_price}")
        print(f"  asset:      {m.asset}")
        print()

    missing = {"BTC", "ETH", "SOL", "XRP"} - assets_seen
    if missing:
        print(f"[i] Assets not seen this cycle: {', '.join(sorted(missing))}")
    else:
        print("[OK] All 4 assets discovered in this scan cycle")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
