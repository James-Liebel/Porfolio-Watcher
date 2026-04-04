from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import ClientSession

    from agents.advisor_settings import AdvisorSettings

    from .cases import EventCase

# ── Historical branch: multi-metric average → logistic ─────────────────────
_HISTORY_METRICS = frozenset({"signal", "signal_7d", "signal_1d"})


def _logistic(x: float) -> float:
    return max(0.02, min(0.98, 1.0 / (1.0 + math.exp(-x))))


def predict_history_signal(case: "EventCase") -> float:
    """
    Pool pre-cutoff numeric rows whose metric is signal / signal_7d / signal_1d.
    Uses the mean of the last up to 4 such values (most recent snapshots), then logistic.
    Other metrics fall back to the legacy single-row behavior. Missing history → 0.5.
    """
    rows = [dict(r) for r in case.history_before]
    tagged = [r for r in rows if str(r.get("metric", "")).strip() in _HISTORY_METRICS]
    if tagged:
        vals = [float(r["value"]) for r in tagged[-4:]]
        v = sum(vals) / len(vals)
        return _logistic(v)
    use = rows
    if not use:
        return 0.5
    v = float(use[-1]["value"])
    return _logistic(v)


def predict_history_shrunk(case: "EventCase", market_weight: float = 0.28) -> float:
    """
    When crypto/multi-horizon signals exist, shrink toward the market-implied YES at cutoff
    (reduces variance vs a pure heuristic). No history → same as predict_history_signal (0.5).
    """
    p_h = predict_history_signal(case)
    rows = [dict(r) for r in case.history_before]
    has_structured = any(str(r.get("metric", "")).strip() in _HISTORY_METRICS for r in rows)
    if not has_structured:
        return p_h
    w = max(0.0, min(0.65, market_weight))
    return max(0.02, min(0.98, w * case.market_yes_price + (1.0 - w) * p_h))


# ── News branch: lexicon + negation + phrase hints + recency weights ───────
_POS_WORDS = frozenset(
    {
        "surge",
        "surges",
        "win",
        "wins",
        "pass",
        "passed",
        "approve",
        "approved",
        "gain",
        "gains",
        "up",
        "bull",
        "bullish",
        "beat",
        "beats",
        "confirmed",
        "confirm",
        "success",
        "successful",
        "likely",
        "expected",
        "projected",
        "rally",
        "rallies",
        "optimism",
        "optimistic",
        "breakthrough",
        "favorable",
        "favourable",
        "victory",
        "lead",
        "leading",
        "support",
        "supports",
        "adopt",
        "adopts",
        "clear",
        "soar",
        "soars",
        "strong",
        "stronger",
        "confidence",
        "confident",
        "secure",
        "secured",
        "advance",
        "advances",
        "progress",
        "positive",
    }
)
_NEG_WORDS = frozenset(
    {
        "crash",
        "crashes",
        "fail",
        "fails",
        "failed",
        "loss",
        "losses",
        "down",
        "bear",
        "bearish",
        "reject",
        "rejected",
        "deny",
        "denies",
        "denied",
        "blocked",
        "block",
        "delay",
        "delayed",
        "delays",
        "slump",
        "concern",
        "concerns",
        "lawsuit",
        "cancel",
        "cancelled",
        "canceled",
        "postpone",
        "postponed",
        "unlikely",
        "risk",
        "risks",
        "warning",
        "veto",
        "setback",
        "grim",
        "weak",
        "weaker",
        "negative",
        "collapse",
        "collapses",
        "plunge",
        "plunges",
        "rejects",
        "struggle",
        "struggles",
    }
)
_NEGATORS = frozenset(
    {
        "not",
        "no",
        "never",
        "without",
        "barely",
        "hardly",
    }
)
_POS_PHRASES = (
    "green light",
    "odds favor",
    "odds favour",
    "more likely",
    "front runner",
    "front-runner",
    "all time high",
    "all-time high",
)
_NEG_PHRASES = (
    "no deal",
    "not expected",
    "rules out",
    "pulls out",
    "shuts down",
    "at risk",
    "less likely",
)

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _phrase_hits(text: str) -> tuple[int, int]:
    pos = sum(1 for p in _POS_PHRASES if p in text)
    neg = sum(1 for p in _NEG_PHRASES if p in text)
    return pos, neg


def _word_sentiment_tokens(tokens: list[str]) -> float:
    """Rough net sentiment in [-1, 1] with simple negation (next few tokens flipped)."""
    score = 0.0
    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        flip = False
        if t == "n't" or (t.endswith("n't") and len(t) > 3):
            flip = True
        elif t in _NEGATORS:
            flip = True
        if flip:
            span = tokens[i + 1 : min(n, i + 5)]
            for j, st in enumerate(span):
                if st in _POS_WORDS:
                    score -= 1.0 / (j + 1.2)
                elif st in _NEG_WORDS:
                    score += 1.0 / (j + 1.2)
            i += 1
            continue
        if t in _POS_WORDS:
            score += 1.0
        elif t in _NEG_WORDS:
            score -= 1.0
        i += 1
    return max(-6.0, min(6.0, score))


def _sentiment_net(text: str) -> float:
    tl = text.lower()
    pp, np = _phrase_hits(tl)
    tokens = _TOKEN_RE.findall(tl)
    wscore = _word_sentiment_tokens(tokens)
    return wscore + 1.4 * pp - 1.4 * np


def predict_news_keywords(case: "EventCase") -> float:
    """
    Lexicon + negation window + multi-word phrases; headlines later in time (closer to cutoff)
    get slightly higher weight.
    """
    rows = list(case.news_before)
    if not rows:
        return 0.5
    total_w = 0.0
    acc = 0.0
    n = len(rows)
    for i, r in enumerate(rows):
        chunk = f"{r.get('headline', '')} {r.get('body', '')}"
        if not chunk.strip():
            continue
        w = 0.55 + 0.45 * (i / max(1, n - 1))
        net = _sentiment_net(chunk)
        acc += w * net
        total_w += w
    if total_w <= 0:
        return 0.5
    # Map typical nets (~[-3,3] over one headline) into probability nudge
    scaled = 0.5 + 0.11 * (acc / total_w)
    return max(0.04, min(0.96, scaled))


_PROB_LINE = re.compile(r"P[_\s]*YES\s*[:=]\s*([0-9]*\.?[0-9]+)", re.I)
_PROB_FLOAT = re.compile(r"\b(0?\.\d+|1\.0+|1)\b")


def _parse_prob_from_llm(text: str) -> float:
    m = _PROB_LINE.search(text)
    if m:
        return max(0.02, min(0.98, float(m.group(1))))
    for m2 in _PROB_FLOAT.finditer(text):
        val = float(m2.group(1))
        if 0.0 <= val <= 1.0:
            return max(0.02, min(0.98, val))
    raise ValueError(f"Could not parse probability from LLM output: {text[:200]!r}")


async def predict_news_llm(
    case: "EventCase",
    session: "ClientSession",
    settings: "AdvisorSettings",
) -> float:
    """Ask Ollama / OpenAI-compatible for P(YES) using only pre-cutoff headlines."""
    from agents.llm_client import complete_chat

    bullets: list[str] = []
    for r in case.news_before:
        bullets.append(f"- {r.get('headline', '').strip()}")
    if not bullets:
        return 0.5

    system = (
        "You estimate the probability that a prediction-market event resolves YES. "
        "Use only the headlines given; no web search. Output exactly one line: P_YES: 0.35 "
        "(a number between 0 and 1)."
    )
    user = (
        f"Event title: {case.title}\n"
        f"Decision cutoff (ignore any information after this): {case.cutoff.isoformat()}\n"
        f"Headlines before cutoff:\n"
        + "\n".join(bullets)
    )
    reply = await complete_chat(session, settings, system, user)
    return _parse_prob_from_llm(reply)
