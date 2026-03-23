from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import Settings  # noqa: E402


def main() -> int:
    if not ENV_PATH.exists():
        print("[X] .env = MISSING")
        print("[X] Fix the above variables before running")
        return 1

    env_values = dotenv_values(ENV_PATH)
    missing = []

    for name, field in Settings.model_fields.items():
        alias = field.alias or name
        value = env_values.get(alias)
        if value is None or str(value).strip() == "":
            print(f"[X] {alias} = MISSING")
            missing.append(alias)
        else:
            print(f"[OK] {alias} = set")

    if missing:
        print("[X] Fix the above variables before running")
        return 1

    print("[OK] Environment complete — ready to run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
