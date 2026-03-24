from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import Settings  # noqa: E402

_LIVE_SECRET_FIELDS = (
    ("polymarket_api_key", "POLYMARKET_API_KEY"),
    ("polymarket_secret", "POLYMARKET_SECRET"),
    ("polymarket_passphrase", "POLYMARKET_PASSPHRASE"),
    ("polymarket_wallet_address", "POLYMARKET_WALLET_ADDRESS"),
    ("wallet_private_key", "WALLET_PRIVATE_KEY"),
)

_OPTIONAL_ALERT_FIELDS = (
    ("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
    ("telegram_chat_id", "TELEGRAM_CHAT_ID"),
)

_CONTROL_FIELDS = (("control_api_token", "CONTROL_API_TOKEN"),)


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    return str(value).strip() == ""


def main() -> int:
    if not ENV_PATH.exists():
        print("[X] .env file missing - copy .env.example to .env")
        return 1

    settings = Settings(_env_file=str(ENV_PATH))

    print("[OK] Loaded settings from .env")
    print(f"     PAPER_TRADE={settings.paper_trade!r}  (structural-arb runtime: python -m src)")

    exit_code = 0

    if settings.paper_trade:
        for _attr, alias in _LIVE_SECRET_FIELDS:
            val = getattr(settings, _attr, "")
            if _is_blank(val):
                print(f"[--] {alias} empty - OK in paper mode")
            else:
                print(f"[OK] {alias} set (not required for paper)")
    else:
        for attr, alias in _LIVE_SECRET_FIELDS:
            val = getattr(settings, attr, "")
            if _is_blank(val):
                print(f"[X] {alias} required when PAPER_TRADE=false")
                exit_code = 1
            else:
                print(f"[OK] {alias} set")

    for attr, alias in _OPTIONAL_ALERT_FIELDS:
        val = getattr(settings, attr, "")
        if _is_blank(val):
            print(f"[--] {alias} empty - Telegram alerts disabled")
        else:
            print(f"[OK] {alias} set")

    for attr, alias in _CONTROL_FIELDS:
        val = getattr(settings, attr, "")
        if _is_blank(val):
            print(f"[--] {alias} empty - control API is open (no token auth)")
        else:
            print(f"[OK] {alias} set (API routes require token)")

    core = (
        ("GAMMA_BASE_URL", settings.gamma_base_url),
        ("CLOB_HOST", settings.clob_host),
        ("ARB_POLL_SECONDS", settings.arb_poll_seconds),
        ("INITIAL_BANKROLL", settings.initial_bankroll),
    )
    for label, val in core:
        print(f"[OK] {label} = {val!r}")

    if exit_code == 0:
        print("[OK] Environment check passed for this mode")
    else:
        print("[X] Fix live trading secrets before PAPER_TRADE=false")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
