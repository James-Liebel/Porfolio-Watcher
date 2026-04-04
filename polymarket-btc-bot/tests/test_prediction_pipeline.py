from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.prediction.cases import EventCase, build_event_cases
from src.prediction.evaluate import compute_prediction_metrics, split_cases_chronologically
from src.prediction.metrics import brier_score, log_loss_binary
from src.prediction.predictors import (
    _parse_prob_from_llm,
    predict_history_shrunk,
    predict_history_signal,
    predict_news_keywords,
)

_FIX = Path(__file__).resolve().parent / "fixtures" / "prediction"


def test_build_event_cases_filters_by_cutoff():
    events = _FIX / "events.jsonl"
    news = _FIX / "news.jsonl"
    history = _FIX / "history.jsonl"
    cases = build_event_cases(events, news, history)
    assert len(cases) == 3
    a = next(c for c in cases if c.event_id == "fixture-a")
    assert len(a.news_before) == 1
    assert len(a.history_before) == 1


def test_history_and_news_predictors_in_unit_interval():
    cases = build_event_cases(_FIX / "events.jsonl", _FIX / "news.jsonl", _FIX / "history.jsonl")
    for c in cases:
        h = predict_history_signal(c)
        n = predict_news_keywords(c)
        assert 0.02 <= h <= 0.98
        assert 0.04 <= n <= 0.96


def test_metrics_finite_on_fixture():
    cases = build_event_cases(_FIX / "events.jsonl", _FIX / "news.jsonl", _FIX / "history.jsonl")
    ys = [c.resolved_yes for c in cases]
    ps = [predict_history_signal(c) for c in cases]
    assert brier_score(ys, ps) >= 0.0
    assert log_loss_binary(ys, ps) == log_loss_binary(ys, ps)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("P_YES: 0.37", 0.37),
        ("Probability P_YES = 0.9", 0.9),
        ("answer: 0.25", 0.25),
    ],
)
def test_parse_prob_from_llm(text, expected):
    got = _parse_prob_from_llm(text)
    assert abs(got - expected) < 1e-6


def test_news_after_cutoff_excluded():
    ev = _FIX / "events.jsonl"
    tmp_news = Path(tempfile.gettempdir()) / "prediction_test_news.jsonl"
    tmp_news.write_text(
        '{"event_id":"fixture-a","time":"2024-06-02T00:00:00+00:00","headline":"late","body":""}\n',
        encoding="utf-8",
    )
    try:
        cases = build_event_cases(ev, tmp_news, None)
        a = next(c for c in cases if c.event_id == "fixture-a")
        assert a.news_before == ()
    finally:
        tmp_news.unlink(missing_ok=True)


def test_predict_history_shrunk_moves_toward_market():
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    c = EventCase(
        event_id="x",
        title="x",
        cutoff=t0,
        resolved_yes=True,
        market_yes_price=0.6,
        news_before=(),
        history_before=(
            {"time": "2023-12-31T00:00:00+00:00", "metric": "signal_7d", "value": 2.0},
        ),
    )
    raw = predict_history_signal(c)
    sh = predict_history_shrunk(c, market_weight=0.5)
    assert abs(sh - 0.6) < abs(raw - 0.6)


def test_split_cases_chronologically():
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    t1 = datetime(2024, 2, 1, tzinfo=timezone.utc)
    t2 = datetime(2024, 3, 1, tzinfo=timezone.utc)
    cases = [
        EventCase("c", "t", t2, True, 0.5, (), ()),
        EventCase("a", "t", t0, False, 0.5, (), ()),
        EventCase("b", "t", t1, True, 0.5, (), ()),
    ]
    tr, te = split_cases_chronologically(cases, 0.7)
    assert [c.event_id for c in tr] == ["a", "b"]
    assert [c.event_id for c in te] == ["c"]


def test_compute_prediction_metrics_runs_on_fixtures():
    cases = build_event_cases(_FIX / "events.jsonl", _FIX / "news.jsonl", _FIX / "history.jsonl")
    out = compute_prediction_metrics(cases, shrink_weight=0.28)
    assert out["n"] == 3
    assert len(out["metrics"]) == 6
    names = {m["name"] for m in out["metrics"]}
    assert "baseline_market" in names


def test_news_negation_lowers_vs_positive_wording():
    t0 = datetime(2024, 1, 10, tzinfo=timezone.utc)
    neg = EventCase(
        event_id="n",
        title="n",
        cutoff=t0,
        resolved_yes=False,
        market_yes_price=0.5,
        news_before=(
            {"time": "2024-01-09T00:00:00+00:00", "headline": "Analysts say win unlikely for challenger", "body": ""},
        ),
        history_before=(),
    )
    pos = EventCase(
        event_id="p",
        title="p",
        cutoff=t0,
        resolved_yes=False,
        market_yes_price=0.5,
        news_before=(
            {"time": "2024-01-09T00:00:00+00:00", "headline": "Analysts say win likely for challenger", "body": ""},
        ),
        history_before=(),
    )
    assert predict_news_keywords(neg) < predict_news_keywords(pos)
