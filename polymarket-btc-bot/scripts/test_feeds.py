from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
import sys
from time import monotonic

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.feeds.aggregator import PriceAggregator  # noqa: E402
from src.feeds.coinbase import CoinbaseFeed  # noqa: E402
from src.feeds.multi_asset import MultiAssetFeed  # noqa: E402


def _fmt_price(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{places}f}"


async def main() -> int:
    multi = MultiAssetFeed()
    coinbase = CoinbaseFeed()
    aggregator = PriceAggregator(multi, coinbase)

    tasks = [
        asyncio.create_task(multi.run()),
        asyncio.create_task(coinbase.run()),
        asyncio.create_task(aggregator.run()),
    ]

    started = monotonic()
    next_print = started + 5
    divergence_triggered = False

    try:
        while monotonic() - started < 30:
            await asyncio.sleep(0.25)
            update = aggregator.latest
            if update is None:
                continue

            btc = update.btc_price
            cb = update.coinbase_btc_price
            div = None
            if btc is not None and cb is not None and btc > 0 and cb > 0:
                div = abs((btc - cb) / ((btc + cb) / 2))
                if div > Decimal("0.003"):
                    divergence_triggered = True

            now = monotonic()
            if now >= next_print:
                next_print += 5
                div_pct = "n/a" if div is None else f"{(float(div) * 100):.3f}%"
                print(
                    "BTC:  Binance=$"
                    f"{_fmt_price(update.btc_price)}  Coinbase=${_fmt_price(update.coinbase_btc_price)}  "
                    f"Median=${_fmt_price(update.median_price)}  Divergence={div_pct}"
                )
                print(f"ETH:  Binance=${_fmt_price(update.eth_price)}")
                print(f"SOL:  Binance=${_fmt_price(update.sol_price)}")
                print(f"XRP:  Binance=${_fmt_price(update.xrp_price, places=4)}")
                print()

        if multi.all_connected and coinbase.connected:
            print("[OK] Both feeds connected and streaming")
            if divergence_triggered:
                print("[OK] Divergence condition (>0.3%) observed during test")
            else:
                print("[i] Divergence >0.3% was not observed in this 30s window")
            return 0

        print("[X] Feed error: one or more feeds were not connected")
        return 1
    except Exception as exc:
        print(f"[X] Feed error: {exc}")
        return 1
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
