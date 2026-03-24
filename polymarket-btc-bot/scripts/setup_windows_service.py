from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=False)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    logs_path = project_root / "logs"
    logs_path.mkdir(parents=True, exist_ok=True)

    nssm_path = Path(r"C:\nssm\nssm.exe")
    if not nssm_path.exists():
        alt = shutil.which("nssm")
        if alt:
            nssm_path = Path(alt)
        else:
            print("Download NSSM from https://nssm.cc/download")
            print(r"Extract nssm.exe to C:\nssm\\")
            print("Then re-run this script")
            return 1

    python_path = Path(sys.executable).resolve()
    # Match docs: `python -m src` from project root (not `python path\to\main.py`).
    _run(
        [
            str(nssm_path),
            "install",
            "polymarket-bot",
            str(python_path),
            "-m",
            "src",
        ]
    )
    _run([str(nssm_path), "set", "polymarket-bot", "AppDirectory", str(project_root)])
    _run([str(nssm_path), "set", "polymarket-bot", "AppStdout", str(logs_path / "bot.log")])
    _run([str(nssm_path), "set", "polymarket-bot", "AppStderr", str(logs_path / "bot_error.log")])
    _run([str(nssm_path), "set", "polymarket-bot", "Start", "SERVICE_AUTO_START"])

    _run(["powercfg", "/change", "standby-timeout-ac", "0"])
    _run(["powercfg", "/change", "monitor-timeout-ac", "0"])
    _run(["powercfg", "/x", "hibernate-timeout", "0"])

    print("Service installed successfully.")
    print("Start it now with: nssm start polymarket-bot")
    print("Check status with: nssm status polymarket-bot")
    print(f"View logs at: {logs_path}")
    print("Your bot will now auto-start on every reboot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
