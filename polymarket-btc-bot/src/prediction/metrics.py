from __future__ import annotations

import math
from typing import Sequence


def brier_score(outcomes: Sequence[bool], probs: Sequence[float]) -> float:
    if len(outcomes) != len(probs) or not outcomes:
        raise ValueError("outcomes and probs must be same non-empty length")
    total = 0.0
    for y, p in zip(outcomes, probs, strict=True):
        yi = 1.0 if y else 0.0
        total += (p - yi) ** 2
    return total / len(outcomes)


def log_loss_binary(outcomes: Sequence[bool], probs: Sequence[float], eps: float = 1e-12) -> float:
    if len(outcomes) != len(probs) or not outcomes:
        raise ValueError("outcomes and probs must be same non-empty length")
    total = 0.0
    for y, p in zip(outcomes, probs, strict=True):
        p = min(1.0 - eps, max(eps, p))
        total += -(math.log(p) if y else math.log(1.0 - p))
    return total / len(outcomes)
