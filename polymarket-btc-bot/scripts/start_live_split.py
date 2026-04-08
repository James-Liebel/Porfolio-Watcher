#!/usr/bin/env python3
"""Compatibility wrapper: the repo now uses a single live arb bot.

Prefer:  python scripts/start_live_arb.py
This file runs the same launcher so old commands keep working.
"""
from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    print(
        "[*] start_live_split.py → single-bot launcher. "
        "Use scripts/start_live_arb.py for clarity.\n"
    )
    runpy.run_path(str(Path(__file__).resolve().parent / "start_live_arb.py"), run_name="__main__")
