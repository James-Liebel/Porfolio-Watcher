from __future__ import annotations

import importlib.util
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def test_conservative_small_bankroll_limits_assets():
    from src.config import Settings

    settings = Settings(
        _env_file=None,
        initial_bankroll=300.0,
        strategy_profile="conservative",
        auto_asset_selection=True,
        trade_btc=True,
        trade_eth=True,
        trade_sol=True,
        trade_xrp=True,
        trade_ada=True,
        trade_doge=True,
        trade_avax=True,
        trade_link=True,
    )

    assert settings.enabled_assets() == ("BTC", "ETH")
    assert settings.is_asset_enabled("BTC") is True
    assert settings.is_asset_enabled("SOL") is False


def test_balanced_small_bankroll_keeps_sol_enabled():
    from src.config import Settings

    settings = Settings(
        _env_file=None,
        initial_bankroll=300.0,
        strategy_profile="balanced",
        auto_asset_selection=True,
        trade_btc=True,
        trade_eth=True,
        trade_sol=True,
        trade_xrp=True,
    )

    assert settings.enabled_assets() == ("BTC", "ETH", "SOL")


def test_manual_asset_flags_win_when_auto_selection_disabled():
    from src.config import Settings

    settings = Settings(
        _env_file=None,
        initial_bankroll=300.0,
        strategy_profile="conservative",
        auto_asset_selection=False,
        trade_btc=True,
        trade_eth=False,
        trade_sol=True,
        trade_xrp=False,
    )

    assert settings.enabled_assets() == ("BTC", "SOL")


def test_paper_trade_report_migrates_reason_and_handles_pending_sample(monkeypatch, capsys):
    repo_root = Path(__file__).resolve().parents[1]
    db_path = repo_root / "data" / f"report-test-{uuid.uuid4().hex}.db"
    try:
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE trades (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp             TEXT NOT NULL,
                market_id             TEXT NOT NULL,
                question              TEXT,
                side                  TEXT NOT NULL,
                bet_size              REAL NOT NULL,
                share_quantity        REAL DEFAULT 0.0,
                limit_price           REAL NOT NULL,
                filled                INTEGER NOT NULL,
                fill_price            REAL,
                outcome               TEXT,
                pnl                   REAL,
                delta                 REAL,
                edge                  REAL,
                true_prob             REAL,
                market_prob           REAL,
                seconds_at_entry      INTEGER,
                paper_trade           INTEGER DEFAULT 0,
                asset                 TEXT DEFAULT 'BTC',
                maker_rebate_earned   REAL DEFAULT 0.0,
                order_type            TEXT DEFAULT 'maker_gtc',
                repost_count          INTEGER DEFAULT 0
            );
            CREATE TABLE daily_summary (
                date TEXT PRIMARY KEY,
                trades INTEGER,
                wins INTEGER,
                losses INTEGER,
                not_filled INTEGER,
                gross_pnl REAL,
                starting_bankroll REAL,
                ending_bankroll REAL
            );
            CREATE TABLE deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                amount REAL NOT NULL,
                note TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO trades (
                timestamp, market_id, question, side, bet_size, share_quantity, limit_price,
                filled, fill_price, outcome, pnl, delta, edge, true_prob, market_prob,
                seconds_at_entry, paper_trade, asset, maker_rebate_earned, order_type, repost_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "m1",
                "Will BTC close higher?",
                "YES",
                10.50,
                21.0,
                0.50,
                1,
                0.50,
                "PENDING",
                None,
                0.004,
                0.08,
                0.60,
                0.52,
                18,
                1,
                "BTC",
                0.0,
                "maker_gtc",
                1,
            ),
        )
        conn.commit()
        conn.close()

        module_path = repo_root / "scripts" / "paper_trade_report.py"
        spec = importlib.util.spec_from_file_location("paper_trade_report_module", module_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class FakeSettings:
            initial_bankroll = 300.0
            strategy_profile = "conservative"
            auto_asset_selection = True

            @staticmethod
            def enabled_assets():
                return ("BTC", "ETH")

        monkeypatch.setattr(module, "DB_PATH", db_path)
        monkeypatch.setattr(module, "_current_settings", lambda: FakeSettings())

        exit_code = module.main(["--days", "7"])
        output = capsys.readouterr().out

        assert exit_code == 0
        assert "NOT ENOUGH DATA" in output
        assert "Resolved fills:        0" in output

        migrated = sqlite3.connect(db_path)
        try:
            cols = {
                row[1]
                for row in migrated.execute("PRAGMA table_info(trades)").fetchall()
            }
        finally:
            migrated.close()
        assert "reason" in cols
    finally:
        if db_path.exists():
            os.remove(db_path)
