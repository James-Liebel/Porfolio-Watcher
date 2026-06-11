"""cycle_diagnostics must distinguish 'priceable' (ignores gates) from 'actionable' (applies the
same gates as scan()). A large priceable/actionable gap is the phantom-edge signal operators chased."""
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
        min_complete_set_edge_bps=10.0,
        min_neg_risk_edge_bps=10.0,
        allow_taker_execution=True,
        paper_taker_fee_bps=0.0,
        paper_spread_penalty_bps=0.0,
        max_basket_notional=50.0,
        max_arb_leg_spread_bps=0.0,
        arb_min_expected_profit_usd=0.0,
        arb_max_plausible_edge_bps=0.0,
        arb_min_leg_touch_notional_usd=0.0,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def _cheap_complete_set(neg_risk: bool) -> tuple[ArbEvent, dict[str, TokenBook]]:
    """Two YES legs whose asks sum to 0.60 -> a real complete-set edge; neg_risk flag toggles
    mutual-exclusivity eligibility."""
    event = ArbEvent(
        event_id="e1",
        title="Who wins?",
        neg_risk=neg_risk,
        markets=[
            OutcomeMarket("e1", "m1", "A?", "A", "y1", "n1", tick_size=0.01),
            OutcomeMarket("e1", "m2", "B?", "B", "y2", "n2", tick_size=0.01),
        ],
    )
    books = {
        "y1": TokenBook("y1", _ts(), 0.29, 0.30, bids=[PriceLevel(0.29, 100)], asks=[PriceLevel(0.30, 100)]),
        "y2": TokenBook("y2", _ts(), 0.29, 0.30, bids=[PriceLevel(0.29, 100)], asks=[PriceLevel(0.30, 100)]),
    }
    return event, books


def test_actionable_equals_scan_when_eligible():
    event, books = _cheap_complete_set(neg_risk=True)
    scanner = OpportunityScanner(_settings())
    diag = scanner.cycle_diagnostics([event], books)
    scan_cs = [o for o in scanner.scan([event], books) if o.strategy_type == "complete_set"]

    assert diag["complete_set_priceable_events"] == 1
    assert diag["complete_set_actionable_events"] == len({o.event_id for o in scan_cs}) == 1
    assert diag["strategy_mode"] == "both"


def test_priceable_but_not_actionable_when_not_mutually_exclusive():
    # Same cheap books, but the event is NOT neg-risk: buying all YES is not a risk-free set, so the
    # mutual-exclusivity gate rejects it. Diagnostics should show priceable>=1 yet actionable==0.
    event, books = _cheap_complete_set(neg_risk=False)
    scanner = OpportunityScanner(_settings())
    diag = scanner.cycle_diagnostics([event], books)

    assert diag["complete_set_priceable_events"] == 1
    assert diag["complete_set_actionable_events"] == 0
    assert scanner.scan([event], books) == []
