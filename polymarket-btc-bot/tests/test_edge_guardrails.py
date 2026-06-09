"""Tests for phantom-edge guardrails: the 'too good to be true' edge ceiling and the
minimum touch-notional depth filter in the opportunity scanner."""
from __future__ import annotations

from datetime import datetime, timezone

from src.arb.models import ArbEvent, OutcomeMarket, PriceLevel, TokenBook
from src.arb.pricing import OpportunityScanner
from src.config import Settings


def _ts() -> datetime:
    return datetime.now(timezone.utc)


def _settings(**overrides) -> Settings:
    defaults = dict(
        paper_trade=True,
        min_event_liquidity=0.0,
        min_outcomes_per_event=2,
        min_complete_set_edge_bps=10.0,
        min_neg_risk_edge_bps=10.0,
        allow_taker_execution=True,
        paper_taker_fee_bps=0.0,
        paper_maker_rebate_bps=0.0,
        paper_spread_penalty_bps=0.0,
        max_basket_notional=50.0,
        max_arb_leg_spread_bps=0.0,
        arb_min_expected_profit_usd=0.0,
        arb_max_plausible_edge_bps=0.0,
        arb_min_leg_touch_notional_usd=0.0,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def _huge_edge_complete_set(size: float = 100.0) -> tuple[ArbEvent, dict[str, TokenBook]]:
    """Two-outcome neg-risk event; YES asks sum to 0.60 -> ~6666 bps complete-set edge."""
    event = ArbEvent(
        event_id="e1",
        title="Who wins?",
        neg_risk=True,
        markets=[
            OutcomeMarket("e1", "m1", "A?", "A", "y1", "n1", tick_size=0.01),
            OutcomeMarket("e1", "m2", "B?", "B", "y2", "n2", tick_size=0.01),
        ],
    )
    books = {
        "y1": TokenBook("y1", _ts(), 0.29, 0.30, bids=[PriceLevel(0.29, size)], asks=[PriceLevel(0.30, size)]),
        "y2": TokenBook("y2", _ts(), 0.29, 0.30, bids=[PriceLevel(0.29, size)], asks=[PriceLevel(0.30, size)]),
    }
    return event, books


def test_plausible_edge_ceiling_rejects_too_good_to_be_true():
    event, books = _huge_edge_complete_set()

    # Disabled ceiling: the (idealized) huge-edge arb is detected.
    cfg_off = _settings(arb_max_plausible_edge_bps=0.0)
    found_off = [o for o in OpportunityScanner(cfg_off).scan([event], books) if o.strategy_type == "complete_set"]
    assert len(found_off) == 1
    assert found_off[0].net_edge_bps > 1000.0

    # Enabled ceiling at 1000 bps: the implausible edge is rejected.
    cfg_on = _settings(arb_max_plausible_edge_bps=1000.0)
    found_on = [o for o in OpportunityScanner(cfg_on).scan([event], books) if o.strategy_type == "complete_set"]
    assert found_on == []


def test_min_touch_notional_rejects_dust_books():
    # Only 1 share resting at each touch -> ~$0.30 notional per leg.
    event, books = _huge_edge_complete_set(size=1.0)

    # No touch-notional floor: still detected (ceiling off).
    cfg_off = _settings(arb_min_leg_touch_notional_usd=0.0)
    found_off = [o for o in OpportunityScanner(cfg_off).scan([event], books) if o.strategy_type == "complete_set"]
    assert len(found_off) == 1

    # Require >= $5 of resting notional at the touch: dust book is filtered out.
    cfg_on = _settings(arb_min_leg_touch_notional_usd=5.0)
    found_on = [o for o in OpportunityScanner(cfg_on).scan([event], books) if o.strategy_type == "complete_set"]
    assert found_on == []
