"""
Record live arb-engine cycles, verify deterministic replay, and write metrics for analysis.

Output directory (default data/backtests/bt-<utc-timestamp>/):
  cycles.jsonl     — full cycle snapshots (same schema as record_arb_session.py)
  summary.json     — aggregates + replay mismatch count
  cycles_metrics.csv — flat per-cycle metrics for spreadsheets

Usage (from polymarket-btc-bot):
  .venv\\Scripts\\python.exe scripts\\rigorous_backtest.py --cycles 40 --sleep-after-cycle 1
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
from src.config import get_settings  # noqa: E402
from src.storage.db import Database  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Record cycles, replay-verify, export backtest metrics.")
    p.add_argument("--cycles", type=int, default=30, help="Number of live engine cycles to record.")
    p.add_argument(
        "--sleep-after-cycle",
        type=float,
        default=1.0,
        help="Seconds to sleep after each cycle (reduces API pressure).",
    )
    p.add_argument(
        "--out-dir",
        type=str,
        default="",
        help="Output directory. Default: data/backtests/bt-<timestamp>/",
    )
    p.add_argument(
        "--skip-replay",
        action="store_true",
        help="Only record; do not run replay verification (faster, less rigorous).",
    )
    p.add_argument(
        "--strict-replay",
        action="store_true",
        help="Exit with code 2 if any replay cycle mismatches recorded canonical state.",
    )
    return p.parse_args()


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


async def _main() -> int:
    args = _parse_args()
    if args.cycles < 1:
        print("--cycles must be >= 1")
        return 1

    config = get_settings()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else ROOT / "data" / "backtests" / f"bt-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    db_path = out_dir / "backtest_session.db"
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
            for cycle_index in range(1, args.cycles + 1):
                await engine.run_cycle()
                snapshot = engine.cycle_snapshot()
                snapshot["record_type"] = "cycle"
                snapshot["schema_version"] = 3
                snapshot["cycle_index"] = cycle_index
                handle.write(json.dumps(snapshot, default=str) + "\n")
                handle.flush()
                flat_rows.append(_flatten_cycle(snapshot))
                if args.sleep_after_cycle > 0 and cycle_index < args.cycles:
                    await asyncio.sleep(args.sleep_after_cycle)
    finally:
        await engine.shutdown()

    replay_result: dict[str, Any] | None = None
    if not args.skip_replay:
        records = load_cycle_records(jsonl_path)
        replay_result = await replay_cycle_records(records, config)

    # Aggregates
    total_opp = sum(int(r.get("opportunities") or 0) for r in flat_rows)
    total_ex = sum(int(r.get("executed") or 0) for r in flat_rows)
    synth_cycles = sum(1 for r in flat_rows if int(r.get("books_synthetic") or 0) > 0)
    eq_start = flat_rows[0].get("equity") if flat_rows else None
    eq_end = flat_rows[-1].get("equity") if flat_rows else None
    cs_bps_vals = [r["max_raw_cs_bps"] for r in flat_rows if r.get("max_raw_cs_bps") is not None]
    nr_bps_vals = [r["max_raw_nr_bps"] for r in flat_rows if r.get("max_raw_nr_bps") is not None]

    summary: dict[str, Any] = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "cycles_recorded": args.cycles,
        "config_snapshot": {
            "max_tracked_events": config.max_tracked_events,
            "min_complete_set_edge_bps": config.min_complete_set_edge_bps,
            "min_neg_risk_edge_bps": config.min_neg_risk_edge_bps,
            "max_basket_notional": config.max_basket_notional,
            "initial_bankroll": config.initial_bankroll,
            "paper_trade": config.paper_trade,
        },
        "aggregates": {
            "sum_opportunities_across_cycles": total_opp,
            "sum_executed_across_cycles": total_ex,
            "cycles_with_any_synthetic_books": synth_cycles,
            "equity_first": eq_start,
            "equity_last": eq_end,
            "max_raw_cs_bps_max": max(cs_bps_vals) if cs_bps_vals else None,
            "max_raw_cs_bps_min": min(cs_bps_vals) if cs_bps_vals else None,
            "max_raw_nr_bps_max": max(nr_bps_vals) if nr_bps_vals else None,
            "max_raw_nr_bps_min": min(nr_bps_vals) if nr_bps_vals else None,
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
            "cycles_checked": len(replay_result["cycles"]),
            "all_matched": replay_result["mismatch_count"] == 0,
        }
        summary["replay_cycle_details"] = [
            {
                "cycle_index": c["cycle_index"],
                "matched": c["matched"],
                "mismatch_fields": c["mismatch_fields"],
            }
            for c in replay_result["cycles"]
        ]
    else:
        summary["replay_verification"] = {"skipped": True}

    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    if flat_rows:
        fieldnames = list(flat_rows[0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(flat_rows)

    print(f"Wrote {args.cycles} cycles to {jsonl_path}")
    print(f"Metrics CSV: {csv_path}")
    print(f"Summary: {summary_path}")
    if replay_result is not None:
        print(
            f"Replay verification: mismatch_count={replay_result['mismatch_count']} "
            f"({'PASS' if replay_result['mismatch_count'] == 0 else 'FAIL'})"
        )
        if args.strict_replay and replay_result["mismatch_count"]:
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
