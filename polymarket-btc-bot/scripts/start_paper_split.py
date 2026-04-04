#!/usr/bin/env python3
"""
Kill prior listeners on the control/advisor ports, wipe dual-agent SQLite files,
start two isolated paper traders:
  - Agent A (8765): structural arbitrage only ($100)
  - Agent B (8767): structural arb + directional overlay with Ollama news ($100)
Then start the LLM advisor (8780) and try `ollama serve` if needed.

Usage (repo root):  python scripts/start_paper_split.py
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

OLLAMA_PORT = 11434

# $100 bankroll sizing (matches scripts/run_two_structural_agents.py)
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
    # Wider universe scan (advisor tuning): more Gamma rows before CLOB book cap.
    "MAX_TRACKED_EVENTS": "500",
}


def _win_kill_port(port: int) -> None:
    ps = (
        f"$c = Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue "
        f"| Select-Object -First 1; if ($c) {{ Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue }}"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )


def kill_listen_ports(ports: list[int]) -> None:
    if sys.platform == "win32":
        for p in ports:
            _win_kill_port(p)
    else:
        for p in ports:
            subprocess.run(["sh", "-c", f"fuser -k {p}/tcp 2>/dev/null || true"], cwd=str(ROOT))


def unlink_sqlite(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix) if suffix else path
        try:
            p.unlink(missing_ok=True)
        except OSError:
            if p.is_file():
                p.unlink()


def _tcp_listening(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_ollama_server(popen_kw: dict) -> subprocess.Popen | None:
    """Start `ollama serve` if Ollama is installed and nothing is listening on 11434."""
    if _tcp_listening("127.0.0.1", OLLAMA_PORT):
        print(f"Ollama already reachable on 127.0.0.1:{OLLAMA_PORT}")
        return None
    exe = shutil.which("ollama")
    if not exe:
        print(
            "Ollama not found in PATH. Install from https://ollama.com then run "
            "`ollama serve` and `ollama pull llama3.2` (or set LLM_PROVIDER / model in .env)."
        )
        return None
    proc = subprocess.Popen([exe, "serve"], cwd=str(ROOT), **popen_kw)
    print(f"Started `ollama serve` (PID {proc.pid})")
    for _ in range(30):
        if _tcp_listening("127.0.0.1", OLLAMA_PORT):
            print(f"Ollama listening on 127.0.0.1:{OLLAMA_PORT}")
            return proc
        time.sleep(0.3)
    print("Warning: Ollama did not open port 11434 in time; advisor may fail until it is up.")
    return proc


def advisor_env(agent_a_port: int, agent_b_port: int) -> dict[str, str]:
    e = os.environ.copy()
    e.update(
        {
            "LLM_PROVIDER": "ollama",
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_MODEL": os.environ.get("OLLAMA_MODEL", "llama3.2"),
            "ADVISOR_HOST": "127.0.0.1",
            "ADVISOR_PORT": "8780",
            "AGENT_A_PORT": str(agent_a_port),
            "AGENT_B_PORT": str(agent_b_port),
        }
    )
    return e


def _ollama_llm_env() -> dict[str, str]:
    """Env for trading process that calls predict_news_llm (overlay sleeve)."""
    return {
        "LLM_PROVIDER": "ollama",
        "OLLAMA_BASE_URL": os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        "OLLAMA_MODEL": os.environ.get("OLLAMA_MODEL", "llama3.2"),
    }


def arb_only_env(port: int, rel_db: str, name: str) -> dict[str, str]:
    e = os.environ.copy()
    db_path = (ROOT / rel_db).resolve()
    e.update(_SHARED_ARB)
    e.update(
        {
            "CONTROL_API_PORT": str(port),
            "ARB_SQLITE_PATH": str(db_path),
            "AGENT_DISPLAY_NAME": name,
            "ENABLE_DIRECTIONAL_OVERLAY": "false",
        }
    )
    return e


def ollama_overlay_env(port: int, rel_db: str, name: str) -> dict[str, str]:
    e = os.environ.copy()
    db_path = (ROOT / rel_db).resolve()
    e.update(_SHARED_ARB)
    e.update(_ollama_llm_env())
    e.update(
        {
            "CONTROL_API_PORT": str(port),
            "ARB_SQLITE_PATH": str(db_path),
            "AGENT_DISPLAY_NAME": name,
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
    return e


def main() -> int:
    agents: list[tuple[int, str, dict[str, str]]] = [
        (8765, "data/arb_agent_1.db", arb_only_env(8765, "data/arb_agent_1.db", "Arbitrage (paper)")),
        (8767, "data/arb_agent_2.db", ollama_overlay_env(8767, "data/arb_agent_2.db", "Ollama overlay (paper)")),
    ]
    kill_listen_ports([8765, 8767, 8778, 8780])
    time.sleep(1.5)

    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    for _, rel, _ in agents:
        unlink_sqlite((ROOT / rel).resolve())

    popen_kw: dict = {}
    if sys.platform == "win32":
        popen_kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    for port, rel, env in agents:
        subprocess.Popen(
            [sys.executable, "-m", "src"],
            cwd=str(ROOT),
            env=env,
            **popen_kw,
        )
        label = env.get("AGENT_DISPLAY_NAME", rel)
        print(f"Started {label!r} on port {port}  db={rel}")

    ensure_ollama_server(popen_kw)
    time.sleep(1.0)

    subprocess.Popen(
        [sys.executable, "-m", "agents.advisor_app"],
        cwd=str(ROOT),
        env=advisor_env(agents[0][0], agents[1][0]),
        **popen_kw,
    )
    print("Started LLM advisor on http://127.0.0.1:8780 (LLM_PROVIDER=ollama)")
    if not _tcp_listening("127.0.0.1", OLLAMA_PORT):
        print("Ollama is not on 127.0.0.1:11434 — install Ollama, run `ollama serve`, then `ollama pull llama3.2`.")

    split_url = f"http://127.0.0.1:{agents[0][0]}/split"
    print()
    print(f"Open split UI: {split_url}")
    print(
        "Direct: http://127.0.0.1:8765/ui/agents-split.html?left=8765&right=8767"
        "&l1=Arbitrage&l2=Ollama%20overlay"
    )
    print("Advisor health: http://127.0.0.1:8780/health")
    time.sleep(5)
    try:
        import webbrowser

        webbrowser.open(split_url)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
