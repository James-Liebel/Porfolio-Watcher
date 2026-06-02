from __future__ import annotations

import os
import tempfile

import pytest

from src.arb.engine import ArbEngine
from src.arb.exchange import PaperExchange
from src.arb.live_readiness import (
    ReadinessThresholds,
    evaluate_live_readiness,
)
from src.arb.models import BasketRecord, utc_now
from src.arb.repository import ArbRepository
from src.config import Settings
from src.storage.db import Database


def _baskets(settled=0, closed=0, failed=0, open_=0, executing=0, pnl_each=0.0):
    rows: list[dict] = []
    for status, n in (
        ("SETTLED", settled),
        ("CLOSED", closed),
        ("FAILED", failed),
        ("OPEN", open_),
        ("EXECUTING", executing),
    ):
        for _ in range(n):
            rows.append({"status": status, "realized_net_pnl": pnl_each})
    return rows


def test_ready_when_all_criteria_met():
    rows = _baskets(settled=200, pnl_each=0.05)  # +$10 net, 100% completion
    report = evaluate_live_readiness(rows)
    assert report.resolved_baskets == 200
    assert report.completion_rate == 1.0
    assert report.net_realized_pnl > 0
    assert report.ready is True
    assert report.failure_reasons() == []


def test_not_ready_when_too_few_resolved():
    rows = _baskets(settled=199, pnl_each=0.05)
    report = evaluate_live_readiness(rows)
    assert report.ready is False
    assert any("resolved_baskets" in r for r in report.failure_reasons())


def test_not_ready_when_completion_rate_below_floor():
    # 200 settled (built) + 5 failed → completion 200/205 ≈ 0.9756 < 0.99
    rows = _baskets(settled=200, failed=5, pnl_each=0.10)
    report = evaluate_live_readiness(rows)
    assert report.failed_baskets == 5
    assert report.completion_rate is not None and report.completion_rate < 0.99
    assert report.ready is False
    assert any("completion_rate" in r for r in report.failure_reasons())


def test_not_ready_when_pnl_not_positive():
    # Enough resolved + perfect completion, but net PnL is negative.
    rows = _baskets(settled=200, pnl_each=-0.01)
    report = evaluate_live_readiness(rows)
    assert report.net_realized_pnl < 0
    assert report.ready is False
    assert any("net_realized_pnl" in r for r in report.failure_reasons())


def test_executing_baskets_excluded_from_attempts():
    rows = _baskets(settled=200, executing=10, pnl_each=0.05)
    report = evaluate_live_readiness(rows)
    assert report.in_flight_baskets == 10
    assert report.attempted_baskets == 200  # EXECUTING not counted
    assert report.ready is True


def test_open_baskets_count_as_built_but_not_resolved():
    # 150 settled + 60 open → 210 built, but only 150 resolved.
    rows = _baskets(settled=150, open_=60, pnl_each=0.05)
    report = evaluate_live_readiness(rows)
    assert report.resolved_baskets == 150
    assert report.built_baskets == 210
    assert report.completion_rate == 1.0
    # default min_resolved=200 → 150 fails
    assert report.ready is False


def test_thresholds_can_be_relaxed_for_pilot():
    rows = _baskets(settled=20, failed=1, pnl_each=0.10)
    th = ReadinessThresholds(
        min_resolved_baskets=10,
        min_completion_rate=0.90,
        min_net_pnl_usd=0.0,
        require_positive_net_pnl=True,
    )
    report = evaluate_live_readiness(rows, th)
    assert report.ready is True


def test_empty_history_is_not_ready():
    report = evaluate_live_readiness([])
    assert report.completion_rate is None
    assert report.ready is False


# ── Engine-level gate integration ───────────────────────────────────────────


def _live_settings(tmpdir: str, **overrides) -> Settings:
    defaults = dict(
        _env_file=None,
        paper_trade=False,
        arb_live_execution=True,
        allow_taker_execution=True,
        initial_bankroll=100.0,
        control_api_port=18799,
        log_level="WARNING",
        paper_spread_penalty_bps=0.0,
        arb_require_live_readiness_proof=True,
        arb_readiness_proof_db=os.path.join(tmpdir, "proof.db"),
        arb_readiness_min_resolved_baskets=1,
        arb_readiness_min_completion_rate=0.9,
        arb_readiness_min_net_pnl_usd=0.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _engine(config: Settings, tmpdir: str) -> ArbEngine:
    repo = ArbRepository(path=os.path.join(tmpdir, "engine.db"))
    legacy = Database(path=os.path.join(tmpdir, "legacy.db"))
    # Inject a PaperExchange so the gate runs without building a live CLOB client.
    return ArbEngine(config=config, legacy_db=legacy, repository=repo, exchange=PaperExchange(config))


async def _seed_settled_baskets(proof_db: str, n: int, pnl_each: float) -> None:
    repo = ArbRepository(path=proof_db)
    await repo.init()
    for i in range(n):
        await repo.create_basket(
            BasketRecord(
                basket_id=f"b-{i}",
                opportunity_id=f"o-{i}",
                event_id=f"e-{i}",
                strategy_type="complete_set",
                status="SETTLED",
                capital_reserved=10.0,
                target_net_edge_bps=100.0,
                realized_net_pnl=pnl_each,
                created_at=utc_now(),
                closed_at=utc_now(),
            )
        )


@pytest.mark.anyio
async def test_live_gate_blocks_without_proof():
    with tempfile.TemporaryDirectory() as tmp:
        config = _live_settings(tmp)
        engine = _engine(config, tmp)
        with pytest.raises(ValueError, match="readiness gate"):
            await engine._enforce_live_readiness_gate()


@pytest.mark.anyio
async def test_live_gate_passes_with_proof():
    with tempfile.TemporaryDirectory() as tmp:
        config = _live_settings(tmp)
        await _seed_settled_baskets(config.arb_readiness_proof_db, n=3, pnl_each=0.50)
        engine = _engine(config, tmp)
        # Should not raise.
        await engine._enforce_live_readiness_gate()


@pytest.mark.anyio
async def test_live_gate_explicit_override_allows_start():
    with tempfile.TemporaryDirectory() as tmp:
        config = _live_settings(tmp, arb_allow_live_without_readiness_proof=True)
        engine = _engine(config, tmp)
        # Empty proof DB, but explicit override → no raise.
        await engine._enforce_live_readiness_gate()


@pytest.mark.anyio
async def test_live_gate_blocks_on_unprofitable_proof():
    with tempfile.TemporaryDirectory() as tmp:
        config = _live_settings(tmp)
        await _seed_settled_baskets(config.arb_readiness_proof_db, n=5, pnl_each=-0.20)
        engine = _engine(config, tmp)
        with pytest.raises(ValueError, match="net_realized_pnl"):
            await engine._enforce_live_readiness_gate()
