"""
Launch two isolated structural-arb traders + optional LLM advisor.

Split UI: frontend/agents-split.html

Ollama (free local): install Ollama, `ollama serve`, `ollama pull llama3.2`, set LLM_PROVIDER=ollama.

OpenAI-compatible (Groq free tier, OpenRouter, etc.): LLM_PROVIDER=openai_compatible + OPENAI_API_KEY + OPENAI_API_BASE.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _popen_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Two $100 paper structural agents + optional LLM advisor.")
    parser.add_argument(
        "--no-advisor",
        action="store_true",
        help="Do not start agents.advisor_app (no Ollama/API needed).",
    )
    args = parser.parse_args()

    agents: list[tuple[int, str, str]] = [
        (8765, "data/arb_agent_1.db", "Structural · A"),
        (8767, "data/arb_agent_2.db", "Structural · B"),
    ]

    env_base = os.environ.copy()
    agent_procs: list[subprocess.Popen] = []

    for port, rel_db, display in agents:
        env = env_base.copy()
        env["CONTROL_API_PORT"] = str(port)
        env["ARB_SQLITE_PATH"] = str((ROOT / rel_db).resolve())
        env["INITIAL_BANKROLL"] = "100"
        env["AGENT_DISPLAY_NAME"] = display
        # Sized for ~$100 bankroll (overrides for this launcher only)
        env["MAX_BASKET_NOTIONAL"] = "20"
        env["MAX_TOTAL_OPEN_BASKETS"] = "2"
        env["MAX_EVENT_EXPOSURE_PCT"] = "0.12"
        env["MAX_OPPORTUNITIES_PER_CYCLE"] = "2"
        proc = subprocess.Popen(
            [sys.executable, "-m", "src"],
            cwd=str(ROOT),
            env=env,
            **_popen_kwargs(),
        )
        agent_procs.append(proc)
        print(f"Started agent {display!r} on port {port} (PID {proc.pid}, db {rel_db})")

    advisor_proc: subprocess.Popen | None = None
    if not args.no_advisor:
        print("Waiting for control APIs to bind…")
        time.sleep(4)
        advisor_proc = subprocess.Popen(
            [sys.executable, "-m", "agents.advisor_app"],
            cwd=str(ROOT),
            env=env_base,
            **_popen_kwargs(),
        )
        print(f"Started LLM advisor (PID {advisor_proc.pid}) on http://127.0.0.1:8780")

    split = (ROOT / "frontend" / "agents-split.html").resolve()
    print()
    print(f"Open split dashboard: {split}")
    print("Stop: Ctrl+C here (agents stopped; advisor stopped if running).")
    print()

    try:
        while True:
            time.sleep(1)
            for i, p in enumerate(agent_procs):
                code = p.poll()
                if code is not None:
                    print(f"Agent {i + 1} exited with code {code}. Shutting down.")
                    raise SystemExit(code)
    except KeyboardInterrupt:
        print("\nStopping agents…")
    finally:
        for p in agent_procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    p.kill()
        if advisor_proc is not None and advisor_proc.poll() is None:
            advisor_proc.terminate()
            try:
                advisor_proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                advisor_proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
