"""
Run two sequential live paper backtests matching the split UI traders ($100 each):

  1) Structural arbitrage only (no directional overlay)
  2) Same arb + directional overlay (optional Ollama news blend via DIRECTIONAL_OVERLAY_LLM_NEWS)

Writes under data/backtests/dual-<utc-stamp>/:
  arb_only/          — cycles.jsonl, cycles_metrics.csv, summary.json, session.db
  ollama_overlay/    — same
  comparison.json    — side-by-side equity / activity (read this first)

**Caveat:** runs are sequential, so Polymarket books move between them — this is an
operational preview, not a perfect controlled A/B on identical snapshots.

Usage (from polymarket-btc-bot):
  python scripts/compare_dual_paper_backtest.py --cycles 15 --sleep-after-cycle 2
  python scripts/compare_dual_paper_backtest.py --cycles 20 --overlay-llm
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.arb import ArbEngine, ArbRepository  # noqa: E402
from src.arb.replay import load_cycle_records, replay_cycle_records  # noqa: E402
from src.config import Settings  # noqa: E402
from src.storage.db import Database  # noqa: E402


def _dual_shared_settings_kwargs() -> dict[str, Any]:
    """Match scripts/start_paper_split.py + run_two_structural_agents.py ($100 sleeve)."""
    return dict(
        _env_file=None,
        paper_trade=True,
        initial_bankroll=100.0,
        paper_taker_fee_bps=50.0,
        paper_spread_penalty_bps=15.0,
        arb_poll_seconds=25,
        max_basket_notional=20.0,
        max_event_exposure_pct=0.12,
        max_total_open_baskets=2,
        max_opportunities_per_cycle=2,
        arb_halt_execution_if_synthetic_books_ge=15,
        min_complete_set_edge_bps=18.0,
        min_neg_risk_edge_bps=28.0,
        arb_min_expected_profit_usd=0.1,
        max_tracked_events=500,
        # Slightly gentler on public APIs during batch backtests
        clob_book_fetch_concurrency=16,
        gamma_http_timeout_seconds=90.0,
        directional_overlay_every_n_cycles=2,
        directional_overlay_only_when_no_arb=True,
        directional_overlay_min_edge=0.06,
        directional_overlay_max_spread=0.14,
        directional_overlay_max_notional=12.0,
        directional_overlay_cash_floor=25.0,
    )


def _diag(record: dict[str, Any]) -> dict[str, Any]:
    lc = (record.get("summary") or {}).get("last_cycle") or {}
    d = lc.get("diagnostics") or {}
    return {
        "max_raw_complete_set_edge_bps": d.get("max_raw_complete_set_edge_bps"),
        "max_raw_neg_risk_edge_bps": d.get("max_raw_neg_risk_edge_bps"),
        "complete_set_priceable_events": d.get("complete_set_priceable_events"),
        "neg_risk_priceable_events": d.get("neg_risk_priceable_events"),
    }


def _flatten_cycle(record: dict[str, Any]) -> dict[str, Any]:
    summ = record.get("summary") or {}
    lc = summ.get("last_cycle") or {}
    dg = _diag(record)
    return {
        "cycle_index": int(record.get("cycle_index", 0)),
        "timestamp": str(record.get("timestamp", "")),
        "equity": summ.get("equity"),
        "cash": summ.get("cash"),
        "realized_pnl": summ.get("realized_pnl"),
        "opportunities": lc.get("opportunities"),
        "executed": lc.get("executed"),
        "tracked_events": lc.get("tracked_events"),
        "books_clob": lc.get("books_clob"),
        "books_synthetic": lc.get("books_synthetic"),
        "books_other": lc.get("books_other"),
        "max_raw_cs_bps": dg.get("max_raw_complete_set_edge_bps"),
        "max_raw_nr_bps": dg.get("max_raw_neg_risk_edge_bps"),
        "cs_priceable": dg.get("complete_set_priceable_events"),
        "nr_priceable": dg.get("neg_risk_priceable_events"),
        "open_baskets": summ.get("open_baskets"),
        "executed_session": summ.get("executed_count"),
        "rejected_session": summ.get("rejected_count"),
    }


async def _run_arm(
    *,
    label: str,
    config: Settings,
    out_dir: Path,
    cycles: int,
    sleep_after: float,
    skip_replay: bool,
    strict_replay: bool,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "session.db"
    jsonl_path = out_dir / "cycles.jsonl"
    summary_path = out_dir / "summary.json"
    csv_path = out_dir / "cycles_metrics.csv"

    legacy_db = Database(str(db_path))
    repository = ArbRepository(str(db_path))
    engine = ArbEngine(config=config, legacy_db=legacy_db, repository=repository)
    await engine.initialize()

    flat_rows: list[dict[str, Any]] = []
    try:
        with jsonl_path.open("w", encoding="utf-8") as handle:
            for cycle_index in range(1, cycles + 1):
                await engine.run_cycle()
                snapshot = engine.cycle_snapshot()
                snapshot["record_type"] = "cycle"
                snapshot["schema_version"] = 3
                snapshot["cycle_index"] = cycle_index
                handle.write(json.dumps(snapshot, default=str) + "\n")
                handle.flush()
                flat_rows.append(_flatten_cycle(snapshot))
                if sleep_after > 0 and cycle_index < cycles:
                    await asyncio.sleep(sleep_after)
    finally:
        await engine.shutdown()

    replay_result: dict[str, Any] | None = None
    if not skip_replay:
        records = load_cycle_records(jsonl_path)
        replay_result = await replay_cycle_records(records, config)

    total_opp = sum(int(r.get("opportunities") or 0) for r in flat_rows)
    total_ex = sum(int(r.get("executed") or 0) for r in flat_rows)
    eq_start = flat_rows[0].get("equity") if flat_rows else None
    eq_end = flat_rows[-1].get("equity") if flat_rows else None
    cash_end = flat_rows[-1].get("cash") if flat_rows else None
    pnl_end = flat_rows[-1].get("realized_pnl") if flat_rows else None

    summary: dict[str, Any] = {
        "label": label,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "cycles_recorded": cycles,
        "config_snapshot": {
            "enable_directional_overlay": config.enable_directional_overlay,
            "directional_overlay_llm_news": config.directional_overlay_llm_news,
            "max_tracked_events": config.max_tracked_events,
            "initial_bankroll": config.initial_bankroll,
            "max_basket_notional": config.max_basket_notional,
        },
        "aggregates": {
            "sum_opportunities_across_cycles": total_opp,
            "sum_executed_across_cycles": total_ex,
            "equity_first": eq_start,
            "equity_last": eq_end,
            "cash_last": cash_end,
            "realized_pnl_last": pnl_end,
        },
        "paths": {
            "jsonl": str(jsonl_path.resolve()),
            "csv": str(csv_path.resolve()),
            "sqlite": str(db_path.resolve()),
        },
    }

    if replay_result is not None:
        summary["replay_verification"] = {
            "mismatch_count": replay_result["mismatch_count"],
            "all_matched": replay_result["mismatch_count"] == 0,
        }
    else:
        summary["replay_verification"] = {"skipped": True}

    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    if flat_rows:
        fieldnames = list(flat_rows[0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(flat_rows)

    print(f"[{label}] wrote {cycles} cycles -> {jsonl_path}")
    if replay_result is not None:
        print(
            f"[{label}] replay mismatches={replay_result['mismatch_count']} "
            f"({'PASS' if replay_result['mismatch_count'] == 0 else 'FAIL'})"
        )
        if strict_replay and replay_result["mismatch_count"]:
            raise RuntimeError(f"{label}: replay mismatch")

    return summary


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compare arb-only vs overlay paper backtests ($100 each).")
    p.add_argument("--cycles", type=int, default=15, help="Cycles per arm (default 15).")
    p.add_argument(
        "--sleep-after-cycle",
        type=float,
        default=2.0,
        help="Pause between cycles to ease API load (default 2s).",
    )
    p.add_argument(
        "--overlay-llm",
        action="store_true",
        help="Use Ollama/OpenAI for overlay news (needs LLM up); else keyword-only overlay.",
    )
    p.add_argument("--skip-replay", action="store_true", help="Skip deterministic replay verification.")
    p.add_argument("--strict-replay", action="store_true", help="Exit non-zero if replay mismatches.")
    p.add_argument(
        "--out-dir",
        type=str,
        default="",
        help="Output root (default data/backtests/dual-<timestamp>).",
    )
    return p.parse_args()


async def _main() -> int:
    args = _parse_args()
    if args.cycles < 1:
        print("--cycles must be >= 1")
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    root = Path(args.out_dir) if args.out_dir else ROOT / "data" / "backtests" / f"dual-{stamp}"
    root.mkdir(parents=True, exist_ok=True)

    base_kw = _dual_shared_settings_kwargs()

    cfg_arb = Settings(
        **base_kw,
        control_api_port=59901,
        agent_display_name="Arbitrage (paper backtest)",
        enable_directional_overlay=False,
        directional_overlay_llm_news=False,
    )
    cfg_overlay = Settings(
        **base_kw,
        control_api_port=59902,
        agent_display_name="Ollama overlay (paper backtest)",
        enable_directional_overlay=True,
        directional_overlay_llm_news=bool(args.overlay_llm),
    )

    print(f"Output root: {root.resolve()}")
    print(
        "Note: arms run one after another; books differ vs a true simultaneous A/B.\n"
        f"Overlay LLM news: {'ON' if args.overlay_llm else 'OFF (keyword overlay only)'}\n"
    )

    s_arb = await _run_arm(
        label="arb_only",
        config=cfg_arb,
        out_dir=root / "arb_only",
        cycles=args.cycles,
        sleep_after=args.sleep_after_cycle,
        skip_replay=args.skip_replay,
        strict_replay=args.strict_replay,
    )
    s_ov = await _run_arm(
        label="ollama_overlay",
        config=cfg_overlay,
        out_dir=root / "ollama_overlay",
        cycles=args.cycles,
        sleep_after=args.sleep_after_cycle,
        skip_replay=args.skip_replay,
        strict_replay=args.strict_replay,
    )

    def _pick(d: dict[str, Any]) -> dict[str, Any]:
        agg = d.get("aggregates") or {}
        return {
            "equity_first": agg.get("equity_first"),
            "equity_last": agg.get("equity_last"),
            "cash_last": agg.get("cash_last"),
            "realized_pnl_last": agg.get("realized_pnl_last"),
            "sum_opportunities": agg.get("sum_opportunities_across_cycles"),
            "sum_executed": agg.get("sum_executed_across_cycles"),
            "replay_mismatches": (d.get("replay_verification") or {}).get("mismatch_count"),
        }

    comparison = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "cycles_per_arm": args.cycles,
        "overlay_llm_news": bool(args.overlay_llm),
        "caveat": "Sequential runs; market snapshots differ between arms.",
        "arb_only": _pick(s_arb),
        "ollama_overlay": _pick(s_ov),
        "paths": {
            "arb_only": str((root / "arb_only").resolve()),
            "ollama_overlay": str((root / "ollama_overlay").resolve()),
        },
    }
    comp_path = root / "comparison.json"
    comp_path.write_text(json.dumps(comparison, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {comp_path}")

    print("\n--- Quick comparison (see comparison.json for full) ---")
    print(json.dumps(comparison, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
