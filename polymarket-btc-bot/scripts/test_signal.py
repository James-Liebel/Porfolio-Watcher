from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_settings  # noqa: E402
from src.execution.sizer import compute_bet_size  # noqa: E402
from src.markets.window import WindowState  # noqa: E402
from src.signal.calculator import compute as compute_signal  # noqa: E402


def _base_window(asset: str = "BTC") -> WindowState:
    now = datetime.now(timezone.utc)
    w = WindowState(
        market_id="m1",
        condition_id="c1",
        question="Test market",
        yes_token_id="y",
        no_token_id="n",
        start_time=now - timedelta(minutes=1),
        end_time=now + timedelta(seconds=30),
        asset=asset,
    )
    w.window_open_price = Decimal("97000") if asset == "BTC" else Decimal("180")
    w.liquidity_yes = 1000.0
    w.liquidity_no = 1000.0
    return w


def main() -> int:
    cfg = get_settings()
    failed = 0

    # Test 1
    w1 = _base_window("BTC")
    w1.current_yes_price = Decimal("0.58")
    w1.current_no_price = Decimal("0.42")
    w1.seconds_remaining = 22
    s1 = compute_signal(w1, Decimal("97485"), cfg, "BTC")
    if s1 and not s1.tradeable and round(s1.delta, 3) == 0.005:
        print("Test 1: PASS")
    else:
        failed += 1
        print(f"Test 1: FAIL — got {s1}")

    # Test 2
    w2 = _base_window("BTC")
    w2.current_yes_price = Decimal("0.52")
    w2.current_no_price = Decimal("0.48")
    w2.seconds_remaining = 22
    s2 = compute_signal(w2, Decimal("97485"), cfg, "BTC")
    if s2 and s2.tradeable and s2.trade_side == "YES":
        print("Test 2: PASS")
    else:
        failed += 1
        print("Test 2: FAIL")

    # Test 3
    w3 = _base_window("BTC")
    w3.current_yes_price = Decimal("0.52")
    w3.current_no_price = Decimal("0.48")
    w3.seconds_remaining = 45
    s3 = compute_signal(w3, Decimal("97485"), cfg, "BTC")
    if s3 and not s3.tradeable:
        print("Test 3: PASS")
    else:
        failed += 1
        print("Test 3: FAIL")

    # Test 4
    w4 = _base_window("BTC")
    w4.current_yes_price = Decimal("0.52")
    w4.current_no_price = Decimal("0.48")
    w4.seconds_remaining = 2
    s4 = compute_signal(w4, Decimal("97485"), cfg, "BTC")
    if s4 and not s4.tradeable:
        print("Test 4: PASS")
    else:
        failed += 1
        print("Test 4: FAIL")

    # Test 5 (SOL uses SOL calibration table + confidence scaling)
    w5 = _base_window("SOL")
    w5.current_yes_price = Decimal("0.45")
    w5.current_no_price = Decimal("0.55")
    w5.seconds_remaining = 18
    s5 = compute_signal(w5, Decimal("181.98"), cfg, "SOL")
    if s5 and s5.tradeable and s5.trade_side == "YES":
        print("Test 5: PASS")
    else:
        failed += 1
        print("Test 5: FAIL")

    # Test 6 — sizing sanity
    w6 = _base_window("BTC")
    w6.current_yes_price = Decimal("0.52")
    w6.current_no_price = Decimal("0.48")
    w6.seconds_remaining = 22
    s6 = compute_signal(w6, Decimal("97485"), cfg, "BTC")
    if s6 is None:
        failed += 1
        print("Test 6: FAIL — no signal")
    else:
        bet = compute_bet_size(s6, Decimal("300.00"), cfg, w6)
        if bet > Decimal("0"):
            print(f"Test 6: PASS - bet size = ${bet}")
        else:
            failed += 1
            print("Test 6: FAIL")

    if failed == 0:
        print("All tests passed [OK]")
        return 0
    print(f"{failed} tests failed — fix before running paper trade")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
