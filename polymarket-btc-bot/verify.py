"""
Quick standalone verification of the key new functionality.
Run: python verify.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

from src.storage.db import Database
from src.risk.manager import RiskManager
from src.config import Settings


def make_settings(**kw):
    defaults = dict(
        paper_trade=True,
        initial_bankroll=300.0,
        edge_threshold=0.07,
        entry_window_seconds=30,
        min_seconds_remaining=3,
        max_bet_fraction=0.06,
        kelly_fraction=0.20,
        target_edge_for_max_size=0.12,
        min_bet_usd=1.0,
        daily_loss_cap=0.10,
        min_market_liquidity=750.0,
        max_concurrent_positions=3,
        max_reposts_per_window=4,
        repost_stale_ticks=2,
        cancel_at_seconds_remaining=6,
        max_maker_aggression_ticks=3,
        maker_rebate_bps_assumption=0.0,
        max_positions_per_asset=1,
        max_total_exposure_pct=0.40,
        control_api_port=18765,
        log_level="WARNING",
    )
    defaults.update(kw)
    return Settings(_env_file=None, **defaults)


async def run():
    errors = []

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(path=db_path)
        await db.init()

        # Test 1: initial deposits = 0
        total = await db.get_total_deposits()
        assert total == 0.0, f"Expected 0.0, got {total}"
        print("[OK] Test 1: initial deposits = $0.00")

        # Test 2: insert two deposits and sum
        await db.insert_deposit(100.0, "first")
        await db.insert_deposit(50.50, "second")
        total = await db.get_total_deposits()
        assert abs(total - 150.50) < 0.001, f"Expected 150.50, got {total}"
        print(f"[OK] Test 2: summed deposits = ${total:.2f}")

        # Test 3: get_deposits returns newest first
        rows = await db.get_deposits()
        assert rows[0]["amount"] == 50.50, f"Expected newest first (50.50), got {rows[0]['amount']}"
        print("[OK] Test 3: get_deposits newest-first OK")

        # Test 4: load_from_db with no daily_summary uses deposits for bankroll init
        fresh_db = Database(path=db_path)
        await fresh_db.init()
        config = make_settings(initial_bankroll=300.0)
        risk = RiskManager(config)
        await risk.load_from_db(fresh_db)
        assert abs(float(risk.current_bankroll) - 150.50) < 0.01, (
            f"Expected bankroll=150.50 from deposits, got {float(risk.current_bankroll)}"
        )
        print(f"[OK] Test 4: load_from_db uses deposits: bankroll=${float(risk.current_bankroll):.2f}")

        # Test 5: add_funds increases bankroll
        before = float(risk.current_bankroll)
        await risk.add_funds(75.0, "test deposit", fresh_db)
        after = float(risk.current_bankroll)
        assert abs(after - (before + 75.0)) < 0.001, f"Expected {before + 75.0}, got {after}"
        print(f"[OK] Test 5: add_funds: ${before:.2f} -> ${after:.2f}")

        # Test 6: add_funds raises on zero
        try:
            await risk.add_funds(0.0, "zero", fresh_db)
            errors.append("Test 6 FAILED: should raise ValueError on amount=0")
        except ValueError:
            print("[OK] Test 6: add_funds rejects zero correctly")

        # Test 7: add_funds raises on negative
        try:
            await risk.add_funds(-10.0, "neg", fresh_db)
            errors.append("Test 7 FAILED: should raise ValueError on negative amount")
        except ValueError:
            print("[OK] Test 7: add_funds rejects negative correctly")

        # Test 8: DB insert_deposit record visible in get_deposits
        all_deposits = await fresh_db.get_deposits()
        amounts = [r["amount"] for r in all_deposits]
        assert 75.0 in amounts, f"New deposit 75.0 not found in {amounts}"
        print(f"[OK] Test 8: add_funds persisted to DB, {len(all_deposits)} deposits total")

        # Test 9: CORS middleware exists and is importable
        from src.control.api import cors_middleware, ControlAPI
        print("[OK] Test 9: cors_middleware and ControlAPI import OK")

        # Test 10: ControlAPI has the new route methods
        config2 = make_settings()
        risk2 = RiskManager(config2)
        ctrl = ControlAPI(config2, risk2, fresh_db)
        assert hasattr(ctrl, "_add_funds"),    "Missing _add_funds method"
        assert hasattr(ctrl, "_funds_history"),"Missing _funds_history method"
        print("[OK] Test 10: ControlAPI has _add_funds and _funds_history methods")

    except AssertionError as e:
        errors.append(f"ASSERTION ERROR: {e}")
    finally:
        os.unlink(db_path)
        try:
            os.unlink(db_path + "-wal")
            os.unlink(db_path + "-shm")
        except Exception:
            pass

    print()
    if errors:
        print("=" * 50)
        for err in errors:
            print(f"[FAIL] {err}")
        print(f"\n{len(errors)} test(s) FAILED")
        sys.exit(1)
    else:
        print("=" * 50)
        print("All 10 tests passed")


if __name__ == "__main__":
    asyncio.run(run())
