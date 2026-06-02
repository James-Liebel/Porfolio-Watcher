"""Certify whether the paper track record is ready for live trading (NEG_RISK_ARB_BLUEPRINT.md §14).

Reads basket history from a paper SQLite DB and prints a pass/fail report against the same
thresholds the engine's live-readiness gate enforces. Exit code 0 = ready, 1 = not ready.

Usage (from polymarket-btc-bot):
  python scripts/check_live_readiness.py --db data/paper_arb.db
  python scripts/check_live_readiness.py --db data/paper_arb.db --min-resolved 200 --min-completion 0.99
  python scripts/check_live_readiness.py --db data/paper_arb.db --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.arb.live_readiness import (  # noqa: E402
    ReadinessThresholds,
    evaluate_live_readiness,
    format_report,
)
from src.arb.repository import ArbRepository  # noqa: E402


async def _run(args: argparse.Namespace) -> int:
    db_path = args.db or os.environ.get("ARB_SQLITE_PATH") or "data/paper_arb.db"
    if not os.path.isfile(db_path):
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 2

    repo = ArbRepository(path=db_path)
    await repo.init()
    rows = await repo.list_baskets(limit=1_000_000)

    thresholds = ReadinessThresholds(
        min_resolved_baskets=args.min_resolved,
        min_completion_rate=args.min_completion,
        min_net_pnl_usd=args.min_net_pnl,
        require_positive_net_pnl=not args.allow_nonpositive_pnl,
    )
    report = evaluate_live_readiness(rows, thresholds)

    if args.json:
        out = report.summary()
        out["proof_db"] = os.path.abspath(db_path)
        print(json.dumps(out, indent=2))
    else:
        print(f"Proof DB: {os.path.abspath(db_path)}")
        print(format_report(report))

    return 0 if report.ready else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="", help="Paper SQLite DB path (default: $ARB_SQLITE_PATH or data/paper_arb.db)")
    p.add_argument("--min-resolved", type=int, default=200)
    p.add_argument("--min-completion", type=float, default=0.99)
    p.add_argument("--min-net-pnl", type=float, default=0.0)
    p.add_argument(
        "--allow-nonpositive-pnl",
        action="store_true",
        help="Only require net PnL ≥ --min-net-pnl (do not also require it strictly > 0).",
    )
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
