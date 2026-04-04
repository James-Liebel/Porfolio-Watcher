"""
Write committed replay fixtures under tests/fixtures/replay/.

Each fixture is a *.jsonl (cycle snapshots) plus *.meta.json with the exact Settings
used when recording, so historical replay stays deterministic without reading .env.

Uses the same scenario as tests.test_arb_system.test_full_engine_replay_reproduces_recorded_cycles.

Usage (from polymarket-btc-bot):
  .venv\\Scripts\\python.exe scripts\\emit_replay_fixtures.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_test_module():
    path = ROOT / "tests" / "test_arb_system.py"
    spec = importlib.util.spec_from_file_location("_arb_replay_fixture_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _emit_complete_set_two_cycles(out_dir: Path) -> None:
    mod = _load_test_module()
    event, books = mod._complete_set_event()
    mod._set_book_source(books, "clob")
    settings = mod._settings(max_opportunities_per_cycle=1, max_basket_notional=3.75)
    resolved_event = mod.ArbEvent(
        event_id=event.event_id,
        title=event.title,
        status="resolved",
        markets=event.markets,
    )

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        db_path = handle.name
    try:
        legacy_db = mod.Database(path=db_path)
        repository = mod.ArbRepository(path=db_path)
        engine = mod.ArbEngine(
            config=settings,
            legacy_db=legacy_db,
            repository=repository,
            universe=mod.StaticUniverse(
                refresh_sequence=[[event], []],
                resolution_map={event.event_id: (resolved_event, "m2", "test-resolution")},
            ),
            market_data=mod.StaticMarketData(books),
        )
        await engine.initialize()
        records = []
        try:
            for cycle_index in (1, 2):
                await engine.run_cycle()
                snapshot = engine.cycle_snapshot()
                snapshot["record_type"] = "cycle"
                snapshot["schema_version"] = 3
                snapshot["cycle_index"] = cycle_index
                records.append(snapshot)
        finally:
            await engine.shutdown()
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass

    base = "complete_set_two_cycles"
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / f"{base}.jsonl"
    meta_path = out_dir / f"{base}.meta.json"

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in records:
            handle.write(json.dumps(row, default=str) + "\n")

    meta = {
        "fixture": base,
        "description": "Two-cycle paper session: complete-set arb then auto-settlement (matches unit test).",
        "settings": json.loads(settings.model_dump_json()),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {jsonl_path}")
    print(f"Wrote {meta_path}")


async def _main() -> int:
    out = ROOT / "tests" / "fixtures" / "replay"
    await _emit_complete_set_two_cycles(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
