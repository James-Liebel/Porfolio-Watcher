from __future__ import annotations

import json
from typing import Any

import aiohttp

from .advisor_settings import AdvisorSettings


def _compact_summary(summary: dict[str, Any] | None, label: str) -> dict[str, Any]:
    if not summary:
        return {"label": label, "error": "unreachable"}
    lc = summary.get("last_cycle") or {}
    diag = lc.get("diagnostics") or {}
    return {
        "label": label,
        "equity": summary.get("equity"),
        "realized_pnl": summary.get("realized_pnl"),
        "trading_halted": summary.get("trading_halted"),
        "halt_reason": summary.get("halt_reason"),
        "tracked_events": summary.get("tracked_events"),
        "latest_opportunities": summary.get("latest_opportunities"),
        "executed_count": summary.get("executed_count"),
        "rejected_count": summary.get("rejected_count"),
        "open_baskets": summary.get("open_baskets"),
        "open_positions": summary.get("open_positions"),
        "last_cycle_opportunities": lc.get("opportunities"),
        "books_clob": lc.get("books_clob"),
        "books_synthetic": lc.get("books_synthetic"),
        "diag_max_cs_bps": diag.get("max_raw_complete_set_edge_bps"),
        "diag_max_nr_bps": diag.get("max_raw_neg_risk_edge_bps"),
        "diag_cs_floor": diag.get("min_complete_set_edge_bps_config"),
        "diag_nr_floor": diag.get("min_neg_risk_edge_bps_config"),
    }


async def fetch_agent_context(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    label: str,
    timeout: float,
) -> dict[str, Any]:
    url = f"http://{host}:{port}/summary"
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status >= 400:
                return _compact_summary(None, label)
            data = await resp.json()
            if isinstance(data, dict):
                return _compact_summary(data, label)
    except Exception as exc:
        return {"label": label, "error": str(exc)}
    return _compact_summary(None, label)


def build_user_prompt(a: dict[str, Any], b: dict[str, Any]) -> str:
    return (
        "Here is JSON for two isolated Polymarket **paper** structural-arbitrage bots "
        "(complete-set + neg-risk scanning, not directional punting). "
        "Respond in **Markdown** with clear sections ### Agent A and ### Agent B.\n"
        "For each: (1) two-sentence health read, (2) data-quality note (CLOB vs synthetic books), "
        "(3) whether edges vs floors explain zero opportunities, (4) one **safe** tuning idea "
        "(env vars like MIN_*_EDGE_BPS, MAX_TRACKED_EVENTS, CLOB_BOOK_FETCH_CONCURRENCY — no trade commands).\n"
        "Keep under 220 words total.\n\n"
        f"```json\n{json.dumps({'agent_a': a, 'agent_b': b}, indent=2, default=str)}\n```"
    )


SYSTEM_PROMPT = (
    "You are a careful assistant for prediction-market infrastructure. "
    "You do not instruct the user to break laws or ToS. "
    "You never output wallet keys or tell the user to paste secrets. "
    "Trading suggestions must be high-level configuration only."
)
