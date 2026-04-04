from __future__ import annotations

from agents.context_builder import _compact_summary, build_user_prompt


def test_compact_summary_unreachable():
    row = _compact_summary(None, "test")
    assert row["label"] == "test"
    assert row["error"] == "unreachable"


def test_compact_summary_minimal():
    row = _compact_summary(
        {
            "equity": 100.0,
            "realized_pnl": 0.0,
            "tracked_events": 5,
            "last_cycle": {"opportunities": 0, "diagnostics": {"max_raw_complete_set_edge_bps": -10}},
        },
        "A",
    )
    assert row["equity"] == 100.0
    assert row["diag_max_cs_bps"] == -10


def test_build_user_prompt_includes_json_keys():
    p = build_user_prompt({"label": "A", "equity": 100}, {"label": "B", "equity": 100})
    assert "agent_a" in p
    assert "agent_b" in p
