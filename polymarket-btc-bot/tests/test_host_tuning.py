"""Bounds and monotonicity for CPU-scaled arb env overrides."""
from __future__ import annotations

from src.arb.host_tuning import structural_bot_env_from_cpu


def test_structural_bot_env_bounds() -> None:
    for mode in ("live", "paper"):
        d = structural_bot_env_from_cpu(mode)  # type: ignore[arg-type]
        assert 9 <= int(d["ARB_POLL_SECONDS"]) <= 22
        assert int(d["CLOB_BOOK_FETCH_CONCURRENCY"]) >= 10
        assert int(d["MAX_TRACKED_EVENTS"]) >= 400
        assert int(d["MAX_OPPORTUNITIES_PER_CYCLE"]) >= 6
        assert int(d["MAX_TOTAL_OPEN_BASKETS"]) >= 5
        assert int(d["MAX_BASKETS_PER_STRATEGY"]) >= 3
        assert int(d["GAMMA_EVENT_MAX_PAGES"]) >= 22
        assert int(d["GAMMA_MARKET_MAX_PAGES"]) >= 34
        assert float(d["GAMMA_HTTP_TIMEOUT_SECONDS"]) >= 60.0


def test_paper_is_tamer_than_live() -> None:
    live = structural_bot_env_from_cpu("live")
    paper = structural_bot_env_from_cpu("paper")
    assert int(paper["CLOB_BOOK_FETCH_CONCURRENCY"]) <= int(live["CLOB_BOOK_FETCH_CONCURRENCY"])
