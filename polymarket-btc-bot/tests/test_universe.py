"""Tests for GammaUniverseService event-building against real Gamma response shapes.

The live /markets endpoint does NOT put a top-level `eventId` on a market — each
market instead carries an `events` list, and the /events endpoint embeds the full
`markets` array per event. A regression here makes the universe silently empty, so
the bot finds nothing to trade. These lock in both shapes.
"""
from __future__ import annotations

import json

import pytest

from src.arb.universe import GammaUniverseService
from src.config import Settings


def _settings(**overrides) -> Settings:
    defaults = dict(
        min_event_liquidity=0.0,
        min_outcomes_per_event=2,
        max_tracked_events=50,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def _service_with_payload(event_rows, market_rows):
    async def fetch_payload():
        return event_rows, market_rows

    return GammaUniverseService(_settings(), fetch_payload=fetch_payload)


def _embedded_market(mid: str, title: str, yes: str, no: str) -> dict:
    return {
        "id": mid,
        "conditionId": f"0x{mid}",
        "question": title,
        "groupItemTitle": title,
        "clobTokenIds": json.dumps([yes, no]),
        "outcomes": json.dumps(["Yes", "No"]),
        "orderPriceMinTickSize": "0.01",
        "feesEnabled": True,
        "liquidity": "5000",
    }


@pytest.mark.anyio
async def test_builds_events_from_embedded_markets():
    """The /events response embeds markets; events must be built from them."""
    event_rows = [
        {
            "id": "900",
            "title": "Who wins the league?",
            "negRisk": True,
            "enableNegRisk": True,
            "negRiskAugmented": False,
            "liquidity": "120000",
            "markets": [
                _embedded_market("1", "Team A", "yesA", "noA"),
                _embedded_market("2", "Team B", "yesB", "noB"),
                _embedded_market("3", "Team C", "yesC", "noC"),
            ],
        }
    ]
    service = _service_with_payload(event_rows, [])
    events = await service.refresh()

    assert len(events) == 1
    event = events[0]
    assert event.event_id == "900"
    assert event.neg_risk is True and event.enable_neg_risk is True
    assert event.neg_risk_augmented is False
    assert len(event.markets) == 3
    assert {m.yes_token_id for m in event.markets} == {"yesA", "yesB", "yesC"}
    assert event.markets[0].tick_size == 0.01
    assert event.markets[0].fees_enabled is True


@pytest.mark.anyio
async def test_groups_standalone_markets_via_events_list():
    """Standalone /markets rows carry an `events` list, not a top-level eventId."""
    event_rows = [
        {"id": "42", "title": "Election", "negRisk": True, "enableNegRisk": True, "liquidity": "80000", "markets": []}
    ]
    market_rows = [
        {
            "id": "10",
            "conditionId": "0x10",
            "question": "Candidate A",
            "groupItemTitle": "Candidate A",
            "clobTokenIds": json.dumps(["y10", "n10"]),
            "events": [{"id": "42"}],  # parent event id lives here, not top-level
            "orderPriceMinTickSize": "0.01",
        },
        {
            "id": "11",
            "conditionId": "0x11",
            "question": "Candidate B",
            "groupItemTitle": "Candidate B",
            "clobTokenIds": json.dumps(["y11", "n11"]),
            "events": [{"id": "42"}],
            "orderPriceMinTickSize": "0.01",
        },
    ]
    service = _service_with_payload(event_rows, market_rows)
    events = await service.refresh()

    assert len(events) == 1
    assert len(events[0].markets) == 2
    assert {m.market_id for m in events[0].markets} == {"10", "11"}


@pytest.mark.anyio
async def test_embedded_and_standalone_markets_dedupe():
    """The same market from both sources must not be double-counted."""
    event_rows = [
        {
            "id": "7",
            "title": "Dup",
            "negRisk": True,
            "liquidity": "50000",
            "markets": [_embedded_market("100", "A", "yA", "nA"), _embedded_market("101", "B", "yB", "nB")],
        }
    ]
    # Same market id 100 also arrives standalone — should merge, not duplicate.
    market_rows = [
        {
            "id": "100",
            "conditionId": "0x100",
            "groupItemTitle": "A",
            "clobTokenIds": json.dumps(["yA", "nA"]),
            "events": [{"id": "7"}],
        }
    ]
    service = _service_with_payload(event_rows, market_rows)
    events = await service.refresh()

    assert len(events) == 1
    assert len(events[0].markets) == 2  # not 3


@pytest.mark.anyio
async def test_standalone_markets_for_unknown_event_are_dropped():
    """Markets whose parent event lacks authoritative /events metadata are skipped."""
    market_rows = [
        {
            "id": "200",
            "conditionId": "0x200",
            "groupItemTitle": "X",
            "clobTokenIds": json.dumps(["yX", "nX"]),
            "events": [{"id": "no-meta-event"}],
        },
        {
            "id": "201",
            "conditionId": "0x201",
            "groupItemTitle": "Y",
            "clobTokenIds": json.dumps(["yY", "nY"]),
            "events": [{"id": "no-meta-event"}],
        },
    ]
    service = _service_with_payload([], market_rows)
    events = await service.refresh()
    assert events == []  # no event metadata -> not built (keeps neg-risk flags honest)


@pytest.mark.anyio
async def test_min_outcomes_filter_applies():
    event_rows = [
        {
            "id": "1",
            "title": "Single",
            "negRisk": True,
            "liquidity": "90000",
            "markets": [_embedded_market("1", "Only", "y", "n")],
        }
    ]
    service = GammaUniverseService(
        _settings(min_outcomes_per_event=2),
        fetch_payload=lambda: _async_payload(event_rows, []),
    )
    events = await service.refresh()
    assert events == []  # one market < min_outcomes_per_event


async def _async_payload(event_rows, market_rows):
    return event_rows, market_rows
