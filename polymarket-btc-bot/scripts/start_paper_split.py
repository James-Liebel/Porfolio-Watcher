#!/usr/bin/env python3
"""Deprecated: use scripts/start_paper_arb.py (same behavior)."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    print(
        "[*] start_paper_split.py is an alias for start_paper_arb.py.\n"
        "    Prefer: python scripts/start_paper_arb.py\n",
        file=sys.stderr,
    )
    runpy.run_path(str(Path(__file__).resolve().parent / "start_paper_arb.py"), run_name="__main__")
