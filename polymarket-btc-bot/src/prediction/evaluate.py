"""Shared metric computation and chronological train/test split for prediction backtests."""

from __future__ import annotations

from typing import Any

from .cases import EventCase
from .metrics import brier_score, log_loss_binary
from .predictors import predict_history_shrunk, predict_history_signal, predict_news_keywords


def split_cases_chronologically(
    cases: list[EventCase],
    train_fraction: float,
) -> tuple[list[EventCase], list[EventCase]]:
    """
    Sort by cutoff time; first floor(n * train_fraction) cases are train, rest test.
    Ensures at least one test row when n >= 2 and train_fraction in (0, 1).
    """
    if not cases:
        return [], []
    f = max(0.0, min(1.0, float(train_fraction)))
    sorted_c = sorted(cases, key=lambda c: c.cutoff)
    n = len(sorted_c)
    if n == 1:
        return sorted_c, []
    k = int(n * f)
    if k <= 0:
        k = 1
    if k >= n:
        k = n - 1
    return sorted_c[:k], sorted_c[k:]


def compute_prediction_metrics(
    cases: list[EventCase],
    shrink_weight: float = 0.28,
) -> dict[str, Any]:
    """Brier / log loss for baseline, historical, news, blends (same as CLI reports)."""
    if not cases:
        return {
            "n": 0,
            "metrics": [],
        }
    ys = [c.resolved_yes for c in cases]
    market_ps = [c.market_yes_price for c in cases]
    h_ps = [predict_history_signal(c) for c in cases]
    hs_ps = [predict_history_shrunk(c, market_weight=shrink_weight) for c in cases]
    n_ps = [predict_news_keywords(c) for c in cases]
    blend = [(h + n) / 2.0 for h, n in zip(h_ps, n_ps, strict=True)]
    blend_s = [(hs + n) / 2.0 for hs, n in zip(hs_ps, n_ps, strict=True)]

    def pack(name: str, ps: list[float]) -> dict[str, Any]:
        return {
            "name": name,
            "brier": brier_score(ys, ps),
            "log_loss": log_loss_binary(ys, ps),
        }

    metrics = [
        pack("baseline_market", market_ps),
        pack("historical_signal", h_ps),
        pack("historical_shrunk", hs_ps),
        pack("news_keywords", n_ps),
        pack("blend_half", blend),
        pack("blend_shrunk_news", blend_s),
    ]
    return {"n": len(cases), "metrics": metrics}
