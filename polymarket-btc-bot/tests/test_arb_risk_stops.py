from __future__ import annotations

from src.arb.exchange import PaperExchange
from src.arb.models import ArbOpportunity, OpportunityLeg
from src.arb.risk import ArbRiskManager
from src.config import Settings


def _opp(event_id: str = "e1", capital: float = 10.0) -> ArbOpportunity:
    return ArbOpportunity(
        strategy_type="complete_set",
        event_id=event_id,
        event_title="t",
        gross_edge_bps=100.0,
        net_edge_bps=100.0,
        capital_required=capital,
        expected_profit=1.0,
        legs=[
            OpportunityLeg(
                market_id="m1",
                token_id="y1",
                outcome_name="A",
                position_side="YES",
                action="BUY",
                price=0.5,
                size=1.0,
                fees_enabled=False,
            )
        ],
        rationale="test",
        requires_conversion=False,
        settle_on_resolution=True,
        cooldown_key="complete_set:e1",
        seconds_to_expiry=86400.0,
    )


def test_synthetic_book_gate_rejects_approve():
    cfg = Settings(
        _env_file=None,
        arb_halt_execution_if_synthetic_books_ge=3,
        allow_taker_execution=True,
        max_basket_notional=100.0,
        max_total_open_baskets=10,
        max_event_exposure_pct=1.0,
        opportunity_cooldown_seconds=0,
    )
    risk = ArbRiskManager(cfg)
    risk.begin_cycle(books_synthetic=5)
    ex = PaperExchange(cfg)
    ex.set_starting_cash(500.0)
    ok, reason = risk.approve(_opp(), ex, 0)
    assert ok is False
    assert "synthetic" in reason.lower()


def test_trailing_equity_stop_halts():
    cfg = Settings(
        _env_file=None,
        arb_trailing_equity_drawdown_pct=0.10,
        daily_loss_cap=0.99,
        allow_taker_execution=True,
        max_basket_notional=100.0,
        max_total_open_baskets=10,
        max_event_exposure_pct=1.0,
        opportunity_cooldown_seconds=0,
    )
    risk = ArbRiskManager(cfg)
    ex = PaperExchange(cfg)
    ex.set_starting_cash(100.0)
    risk.capture_session_baseline(ex)
    risk.begin_cycle(books_synthetic=0)
    assert risk.approve(_opp(capital=5.0), ex, 0)[0] is True
    ex.cash = 85.0
    risk.begin_cycle(books_synthetic=0)
    ok, _ = risk.approve(_opp(capital=5.0), ex, 0)
    assert ok is False
    assert risk.halted
    assert "trailing" in risk.halt_reason.lower()


def test_session_realized_loss_halts():
    cfg = Settings(
        _env_file=None,
        arb_session_realized_loss_usd=5.0,
        daily_loss_cap=0.99,
        arb_trailing_equity_drawdown_pct=0.0,
        allow_taker_execution=True,
        max_basket_notional=100.0,
        max_total_open_baskets=10,
        max_event_exposure_pct=1.0,
        opportunity_cooldown_seconds=0,
    )
    risk = ArbRiskManager(cfg)
    ex = PaperExchange(cfg)
    ex.set_starting_cash(100.0)
    risk.capture_session_baseline(ex)
    ex.realized_pnl = -6.0
    risk.begin_cycle(books_synthetic=0)
    ok, _ = risk.approve(_opp(), ex, 0)
    assert ok is False
    assert "session realized" in risk.halt_reason.lower()


def test_approve_respects_per_cycle_max_basket_override():
    cfg = Settings(
        _env_file=None,
        allow_taker_execution=True,
        max_basket_notional=50.0,
        max_total_open_baskets=10,
        max_event_exposure_pct=1.0,
        opportunity_cooldown_seconds=0,
        daily_loss_cap=0.99,
    )
    risk = ArbRiskManager(cfg)
    risk.begin_cycle(books_synthetic=0)
    ex = PaperExchange(cfg)
    ex.set_starting_cash(500.0)
    assert risk.approve(_opp(capital=60.0), ex, 0)[0] is False
    assert risk.approve(_opp(capital=60.0), ex, 0, max_basket_notional=100.0)[0] is True
