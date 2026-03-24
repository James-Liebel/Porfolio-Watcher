from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.arb.replay import load_cycle_records, replay_cycle_records  # noqa: E402
from src.config import get_settings  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay recorded arb session snapshots through the arb engine.")
    parser.add_argument("path", type=str, help="Path to a JSONL file produced by record_arb_session.py")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any replayed cycle differs from its recorded canonical snapshot.",
    )
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    config = get_settings()
    path = Path(args.path)
    if not path.exists():
        print(f"Replay file not found: {path}")
        return 1

    records = load_cycle_records(path)
    result = await replay_cycle_records(records, config)

    for cycle in result["cycles"]:
        mismatch_fields = ",".join(cycle["mismatch_fields"]) if cycle["mismatch_fields"] else "-"
        status = "OK" if cycle["matched"] else "MISMATCH"
        print(
            f"cycle={cycle['cycle_index']} status={status} mismatch_fields={mismatch_fields}"
        )

    print(f"Replayed {len(result['cycles'])} cycles from {path}")
    if args.strict and result["mismatch_count"]:
        print(f"Replay mismatches: {result['mismatch_count']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
