"""
Launch two isolated paper traders + optional LLM advisor.

- Port 8765: structural arbitrage only ($100)
- Port 8767: structural arb + directional overlay with Ollama news ($100)

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

_SHARED_ARB = {
    "PAPER_TRADE": "true",
    "CONTROL_API_TOKEN": "",
    "INITIAL_BANKROLL": "100",
    "PAPER_TAKER_FEE_BPS": "50",
    "PAPER_SPREAD_PENALTY_BPS": "15",
    "ARB_POLL_SECONDS": "25",
    "MAX_BASKET_NOTIONAL": "20",
    "MAX_EVENT_EXPOSURE_PCT": "0.12",
    "MAX_TOTAL_OPEN_BASKETS": "2",
    "MAX_OPPORTUNITIES_PER_CYCLE": "2",
    "ARB_HALT_EXECUTION_IF_SYNTHETIC_BOOKS_GE": "15",
    "MIN_COMPLETE_SET_EDGE_BPS": "18",
    "MIN_NEG_RISK_EDGE_BPS": "28",
    "ARB_MIN_EXPECTED_PROFIT_USD": "0.1",
    "MAX_TRACKED_EVENTS": "500",
}


def _popen_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {}


def _ollama_llm_env() -> dict[str, str]:
    return {
        "LLM_PROVIDER": os.environ.get("LLM_PROVIDER", "ollama"),
        "OLLAMA_BASE_URL": os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        "OLLAMA_MODEL": os.environ.get("OLLAMA_MODEL", "llama3.2"),
        "OPENAI_API_BASE": os.environ.get("OPENAI_API_BASE", ""),
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
        "OPENAI_MODEL": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
    }


def _arb_only_env(port: int, rel_db: str, display: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(_SHARED_ARB)
    env.update(
        {
            "CONTROL_API_PORT": str(port),
            "ARB_SQLITE_PATH": str((ROOT / rel_db).resolve()),
            "AGENT_DISPLAY_NAME": display,
            "ENABLE_DIRECTIONAL_OVERLAY": "false",
        }
    )
    return env


def _ollama_overlay_env(port: int, rel_db: str, display: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(_SHARED_ARB)
    env.update(_ollama_llm_env())
    env.update(
        {
            "CONTROL_API_PORT": str(port),
            "ARB_SQLITE_PATH": str((ROOT / rel_db).resolve()),
            "AGENT_DISPLAY_NAME": display,
            "ENABLE_DIRECTIONAL_OVERLAY": "true",
            "DIRECTIONAL_OVERLAY_LLM_NEWS": "true",
            "DIRECTIONAL_OVERLAY_EVERY_N_CYCLES": "2",
            "DIRECTIONAL_OVERLAY_ONLY_WHEN_NO_ARB": "true",
            "DIRECTIONAL_OVERLAY_MIN_EDGE": "0.06",
            "DIRECTIONAL_OVERLAY_MAX_SPREAD": "0.14",
            "DIRECTIONAL_OVERLAY_MAX_NOTIONAL": "12",
            "DIRECTIONAL_OVERLAY_CASH_FLOOR": "25",
        }
    )
    return env


def _advisor_env(agent_a_port: int, agent_b_port: int) -> dict[str, str]:
    e = os.environ.copy()
    e.update(
        {
            "ADVISOR_HOST": os.environ.get("ADVISOR_HOST", "127.0.0.1"),
            "ADVISOR_PORT": os.environ.get("ADVISOR_PORT", "8780"),
            "AGENT_A_PORT": str(agent_a_port),
            "AGENT_B_PORT": str(agent_b_port),
            "LLM_PROVIDER": os.environ.get("LLM_PROVIDER", "ollama"),
            "OLLAMA_BASE_URL": os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
            "OLLAMA_MODEL": os.environ.get("OLLAMA_MODEL", "llama3.2"),
            "OPENAI_API_BASE": os.environ.get("OPENAI_API_BASE", ""),
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
            "OPENAI_MODEL": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        }
    )
    return e


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Arbitrage paper trader (8765) + Ollama-overlay paper trader (8767), $100 each; optional LLM advisor."
    )
    parser.add_argument(
        "--no-advisor",
        action="store_true",
        help="Do not start agents.advisor_app (no Ollama/API needed).",
    )
    args = parser.parse_args()

    agents: list[tuple[int, str, dict[str, str]]] = [
        (8765, "data/arb_agent_1.db", _arb_only_env(8765, "data/arb_agent_1.db", "Arbitrage (paper)")),
        (
            8767,
            "data/arb_agent_2.db",
            _ollama_overlay_env(8767, "data/arb_agent_2.db", "Ollama overlay (paper)"),
        ),
    ]

    agent_procs: list[subprocess.Popen] = []

    for port, rel_db, env in agents:
        proc = subprocess.Popen(
            [sys.executable, "-m", "src"],
            cwd=str(ROOT),
            env=env,
            **_popen_kwargs(),
        )
        agent_procs.append(proc)
        print(
            f"Started {env.get('AGENT_DISPLAY_NAME', rel_db)!r} on port {port} "
            f"(PID {proc.pid}, db {rel_db})"
        )

    advisor_proc: subprocess.Popen | None = None
    if not args.no_advisor:
        print("Waiting for control APIs to bind…")
        time.sleep(4)
        advisor_proc = subprocess.Popen(
            [sys.executable, "-m", "agents.advisor_app"],
            cwd=str(ROOT),
            env=_advisor_env(agents[0][0], agents[1][0]),
            **_popen_kwargs(),
        )
        print(f"Started LLM advisor (PID {advisor_proc.pid}) on http://127.0.0.1:8780")

    split_file = (ROOT / "frontend" / "agents-split.html").resolve()
    split_url = (
        f"http://127.0.0.1:{agents[0][0]}/ui/agents-split.html"
        "?left=8765&right=8767&l1=Arbitrage&l2=Ollama%20overlay"
    )
    print()
    print(f"Split dashboard: {split_url}")
    print(f"Or open file: {split_file}")
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
