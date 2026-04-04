"""Pre-event historical + news features for offline prediction backtests (separate from structural arb)."""

from .cases import EventCase, build_event_cases
from .metrics import brier_score, log_loss_binary
from .predictors import predict_history_shrunk, predict_history_signal, predict_news_keywords

__all__ = [
    "EventCase",
    "build_event_cases",
    "brier_score",
    "log_loss_binary",
    "predict_history_shrunk",
    "predict_history_signal",
    "predict_news_keywords",
]
