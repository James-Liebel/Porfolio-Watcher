"""
Offline backtest: pre-event **historical features** and **news** vs resolved outcomes.

This is separate from structural arbitrage. Feed three JSONL files:

1) events.jsonl — one row per resolved market you want to score
   {"event_id":"e1","title":"...","cutoff_time":"2024-01-10T12:00:00+00:00",
    "resolved_yes": true, "market_yes_price": 0.42}

2) news.jsonl — items strictly **before** cutoff_time for that event (timestamps enforced)
   {"event_id":"e1","time":"2024-01-09T15:00:00+00:00","headline":"...","body":""}

3) history.jsonl — numeric features **before** cutoff (e.g. model scores, macro reads)
   {"event_id":"e1","time":"2024-01-09T08:00:00+00:00","metric":"signal","value":0.8}

Predictors (run by default):
  - Historical: mean of latest `signal` / `signal_7d` / `signal_1d` rows → logistic → P(YES)
  - Historical (shrunk): blends that with `market_yes_price` when structured signals exist (--shrink-weight)
  - News: lexicon + negation + phrases + recency weights (no API)

Optional:
  --news-llm  Uses Ollama or OPENAI-compatible from .env (same as agents advisor).

Usage:
  .venv\\Scripts\\python.exe scripts\\run_prediction_backtest.py ^
    --events tests\\fixtures\\prediction\\events.jsonl ^
    --news tests\\fixtures\\prediction\\news.jsonl ^
    --history tests\\fixtures\\prediction\\history.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402

from agents.advisor_settings import AdvisorSettings  # noqa: E402
from src.prediction.cases import EventCase, build_event_cases  # noqa: E402
from src.prediction.evaluate import compute_prediction_metrics, split_cases_chronologically  # noqa: E402
from src.prediction.metrics import brier_score, log_loss_binary  # noqa: E402
from src.prediction.predictors import predict_history_signal, predict_news_llm  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest pre-event history + news predictors.")
    p.add_argument("--events", type=Path, required=True)
    p.add_argument("--news", type=Path, default=None)
    p.add_argument("--history", type=Path, default=None)
    p.add_argument(
        "--news-llm",
        action="store_true",
        help="Also run LLM news predictor (Ollama or openai_compatible via .env).",
    )
    p.add_argument(
        "--shrink-weight",
        type=float,
        default=0.28,
        help="Market blend weight for predict_history_shrunk (0=no shrink).",
    )
    p.add_argument(
        "--train-fraction",
        type=float,
        default=None,
        help="If set (e.g. 0.7), print train vs test metrics by event cutoff (chronological).",
    )
    return p.parse_args()


def _report(name: str, ys: list[bool], ps: list[float]) -> None:
    print(f"{name:22}  Brier={brier_score(ys, ps):.4f}  LogLoss={log_loss_binary(ys, ps):.4f}")


async def _run_llm_block(cases: list[EventCase], ys: list[bool], h_ps: list[float]) -> None:
    settings = AdvisorSettings()
    async with aiohttp.ClientSession() as session:
        llm_ps: list[float] = []
        for i, c in enumerate(cases):
            try:
                p = await predict_news_llm(c, session, settings)
            except Exception as exc:
                print(f"LLM failed event {c.event_id}: {exc}")
                p = predict_news_keywords(c)
            llm_ps.append(p)
            print(f"  [{i+1}/{len(cases)}] {c.event_id} P_yes={p:.3f}")
        _report("News (LLM)", ys, llm_ps)
        blend_l = [(h + l) / 2.0 for h, l in zip(h_ps, llm_ps, strict=True)]
        _report("Blend (H+LLM)/2", ys, blend_l)


def main() -> int:
    args = _parse_args()
    if not args.events.is_file():
        print(f"Missing events file: {args}")
        return 1

    cases = build_event_cases(args.events, args.news, args.history)
    if not cases:
        print("No events loaded.")
        return 1

    ev = compute_prediction_metrics(cases, shrink_weight=args.shrink_weight)
    ys = [c.resolved_yes for c in cases]
    metrics = ev["metrics"]
    by_name = {m["name"]: m for m in metrics}

    print(f"Events: {len(cases)}")
    for key, label in [
        ("baseline_market", "Baseline (market)"),
        ("historical_signal", "Historical (signal)"),
        ("historical_shrunk", "Historical (shrunk)"),
        ("news_keywords", "News (keywords)"),
        ("blend_half", "Blend (H+N)/2"),
        ("blend_shrunk_news", "Blend (Hs+N)/2"),
    ]:
        m = by_name[key]
        print(f"{label:22}  Brier={m['brier']:.4f}  LogLoss={m['log_loss']:.4f}")

    if args.train_fraction is not None and len(cases) >= 2:
        tr, te = split_cases_chronologically(cases, float(args.train_fraction))
        print(
            f"\nTrain/test split: train={len(tr)} test={len(te)} (fraction={args.train_fraction})\n"
        )
        if tr:
            tr_ev = compute_prediction_metrics(tr, shrink_weight=args.shrink_weight)
            for m in tr_ev["metrics"]:
                print(f"  TRAIN {m['name']:<18} Brier={m['brier']:.4f}  LogLoss={m['log_loss']:.4f}")
        if te:
            te_ev = compute_prediction_metrics(te, shrink_weight=args.shrink_weight)
            for m in te_ev["metrics"]:
                print(f"  TEST {m['name']:<18} Brier={m['brier']:.4f}  LogLoss={m['log_loss']:.4f}")

    h_ps = [predict_history_signal(c) for c in cases]

    if args.news_llm:
        asyncio.run(_run_llm_block(cases, ys, h_ps))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
