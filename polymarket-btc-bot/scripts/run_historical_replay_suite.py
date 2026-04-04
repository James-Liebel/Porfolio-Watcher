"""
Batch-replay recorded cycle JSONL files to verify the pricing engine reproduces each session.

Historical captures are JSONL files (same schema as record_arb_session.py / rigorous_backtest.py).
Each file should have a sibling *.meta.json with {"settings": <Settings.model_dump>} from emit_replay_fixtures.py
or from a future recorder update. If meta is missing, use --use-env-settings (loads .env via get_settings()).

Default search paths:
  - tests/fixtures/replay/*.jsonl (committed regression fixtures)
  - optional: data/replays/*.jsonl, data/backtests/*/cycles.jsonl

Usage:
  .venv\\Scripts\\python.exe scripts\\run_historical_replay_suite.py --strict
  .venv\\Scripts\\python.exe scripts\\run_historical_replay_suite.py --include-data-replays --use-env-settings
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.arb.replay import load_cycle_records, replay_cycle_records  # noqa: E402
from src.config import Settings, get_settings  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Replay all historical JSONL session files and report mismatches.")
    p.add_argument(
        "--fixtures-dir",
        type=str,
        default="",
        help="Extra directory to scan for *.jsonl (in addition to defaults unless --only-dir).",
    )
    p.add_argument(
        "--only-dir",
        type=str,
        default="",
        help="If set, only scan this directory for *.jsonl.",
    )
    p.add_argument(
        "--include-data-replays",
        action="store_true",
        help="Also replay data/replays/*.jsonl",
    )
    p.add_argument(
        "--include-backtests",
        action="store_true",
        help="Also replay data/backtests/*/cycles.jsonl",
    )
    p.add_argument(
        "--use-env-settings",
        action="store_true",
        help="When a JSONL has no .meta.json, use get_settings() (.env) instead of skipping.",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any file has replay mismatches or errors.",
    )
    p.add_argument(
        "--report",
        type=str,
        default="",
        help="Write JSON report to this path (default: print summary only).",
    )
    return p.parse_args()


def _settings_from_meta(meta_path: Path) -> Settings:
    raw = json.loads(meta_path.read_text(encoding="utf-8"))
    inner = raw.get("settings")
    if not isinstance(inner, dict):
        raise ValueError(f"{meta_path}: missing settings object")
    return Settings(_env_file=None, **inner)


def _discover_jsonl(args: argparse.Namespace) -> list[Path]:
    paths: list[Path] = []
    if args.only_dir:
        base = Path(args.only_dir)
        paths.extend(sorted(base.glob("*.jsonl")))
        return paths

    paths.extend(sorted((ROOT / "tests" / "fixtures" / "replay").glob("*.jsonl")))
    if args.fixtures_dir:
        paths.extend(sorted(Path(args.fixtures_dir).glob("*.jsonl")))

    if args.include_data_replays:
        dr = ROOT / "data" / "replays"
        if dr.is_dir():
            paths.extend(sorted(dr.glob("*.jsonl")))

    if args.include_backtests:
        bt = ROOT / "data" / "backtests"
        if bt.is_dir():
            for sub in sorted(bt.iterdir()):
                if sub.is_dir():
                    c = sub / "cycles.jsonl"
                    if c.is_file():
                        paths.append(c)

    # de-dupe preserving order
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


async def _replay_one(jsonl_path: Path, use_env_fallback: bool) -> dict[str, Any]:
    meta_path = jsonl_path.with_name(f"{jsonl_path.stem}.meta.json")
    try:
        if meta_path.is_file():
            config = _settings_from_meta(meta_path)
            settings_source = "meta"
        elif use_env_fallback:
            config = get_settings()
            settings_source = "env"
        else:
            return {
                "path": str(jsonl_path),
                "ok": False,
                "skipped": True,
                "reason": f"missing {meta_path.name} (pass --use-env-settings to use .env)",
                "mismatch_count": None,
                "cycles": 0,
            }

        records = load_cycle_records(jsonl_path)
        if not records:
            return {
                "path": str(jsonl_path),
                "ok": False,
                "skipped": False,
                "reason": "no cycle records in file",
                "mismatch_count": None,
                "cycles": 0,
                "settings_source": settings_source,
            }

        result = await replay_cycle_records(records, config)
        mc = result["mismatch_count"]
        return {
            "path": str(jsonl_path),
            "ok": mc == 0,
            "skipped": False,
            "mismatch_count": mc,
            "cycles": len(records),
            "settings_source": settings_source,
            "failures": [
                {"cycle_index": c["cycle_index"], "mismatch_fields": c["mismatch_fields"]}
                for c in result["cycles"]
                if not c["matched"]
            ],
        }
    except Exception as exc:
        return {
            "path": str(jsonl_path),
            "ok": False,
            "skipped": False,
            "reason": str(exc),
            "mismatch_count": None,
            "cycles": 0,
        }


async def _main() -> int:
    args = _parse_args()
    files = _discover_jsonl(args)
    if not files:
        print("No *.jsonl files found.")
        return 1 if args.strict else 0

    rows: list[dict[str, Any]] = []
    for path in files:
        rows.append(await _replay_one(path, args.use_env_settings))

    summary = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "files_checked": len(rows),
        "passed": sum(1 for r in rows if r.get("ok")),
        "failed": sum(1 for r in rows if not r.get("skipped") and not r.get("ok")),
        "skipped": sum(1 for r in rows if r.get("skipped")),
        "results": rows,
    }

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        print(f"Wrote report: {args.report}")

    for r in rows:
        status = "SKIP" if r.get("skipped") else ("PASS" if r.get("ok") else "FAIL")
        extra = ""
        if r.get("mismatch_count") is not None:
            extra = f" mismatches={r['mismatch_count']} cycles={r['cycles']}"
        elif r.get("reason"):
            extra = f" ({r['reason']})"
        print(f"{status} {r['path']}{extra}")

    if args.strict:
        failed = [r for r in rows if not r.get("skipped") and not r.get("ok")]
        if failed:
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
