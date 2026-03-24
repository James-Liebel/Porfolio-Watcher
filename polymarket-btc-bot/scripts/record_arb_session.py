from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.arb import ArbEngine, ArbRepository  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.storage.db import Database  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record arb-engine cycle snapshots to JSONL.")
    parser.add_argument("--cycles", type=int, default=25, help="Number of cycles to record.")
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.0,
        help="Optional sleep between cycles. Defaults to 0 because run_cycle() already polls live data once.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="",
        help="Output JSONL path. Defaults to REPLAY_OUTPUT_DIR/arb-session-<timestamp>.jsonl",
    )
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    config = get_settings()

    output_path = Path(args.output) if args.output else (
        ROOT
        / config.replay_output_dir
        / f"arb-session-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.jsonl"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    legacy_db = Database()
    repository = ArbRepository()
    engine = ArbEngine(config=config, legacy_db=legacy_db, repository=repository)
    await engine.initialize()

    try:
        with output_path.open("w", encoding="utf-8") as handle:
            for cycle_index in range(1, args.cycles + 1):
                await engine.run_cycle()
                snapshot = engine.cycle_snapshot()
                snapshot["record_type"] = "cycle"
                snapshot["schema_version"] = 3
                snapshot["cycle_index"] = cycle_index
                handle.write(json.dumps(snapshot, default=str) + "\n")
                handle.flush()
                if args.sleep_seconds > 0 and cycle_index < args.cycles:
                    await asyncio.sleep(args.sleep_seconds)
    finally:
        await engine.shutdown()

    print(f"Wrote {args.cycles} cycle snapshots to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
