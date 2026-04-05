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


def _l2_creds_ok(settings: Settings) -> bool:
    """All three POLYMARKET_* set, or all empty (derive L2 at runtime from wallet key)."""
    k = (settings.polymarket_api_key or "").strip()
    s = (settings.polymarket_secret or "").strip()
    p = (settings.polymarket_passphrase or "").strip()
    if k and s and p:
        return True
    if not k and not s and not p:
        return True
    return False


def main() -> int:
    if not ENV_PATH.exists():
        print("[X] .env file missing - copy .env.example to .env")
        return 1

    settings = Settings(_env_file=str(ENV_PATH))

    print("[OK] Loaded settings from .env")
    print(f"     PAPER_TRADE={settings.paper_trade!r}  (structural-arb runtime: python -m src)")
    print(f"     ARB_LIVE_EXECUTION={getattr(settings, 'arb_live_execution', False)!r}")

    exit_code = 0

    if getattr(settings, "arb_live_execution", False) and settings.paper_trade:
        print("[X] ARB_LIVE_EXECUTION=true requires PAPER_TRADE=false")
        exit_code = 1

    if getattr(settings, "arb_live_execution", False) and not settings.allow_taker_execution:
        print("[X] ARB_LIVE_EXECUTION=true requires ALLOW_TAKER_EXECUTION=true")
        exit_code = 1

    if settings.paper_trade:
        for _attr, alias in _LIVE_SECRET_FIELDS:
            val = getattr(settings, _attr, "")
            if _is_blank(val):
                print(f"[--] {alias} empty - OK in paper mode")
            else:
                print(f"[OK] {alias} set (not required for paper)")
    else:
        for attr, alias in (
            ("polymarket_wallet_address", "POLYMARKET_WALLET_ADDRESS"),
            ("wallet_private_key", "WALLET_PRIVATE_KEY"),
        ):
            val = getattr(settings, attr, "")
            if _is_blank(val):
                print(f"[X] {alias} required when PAPER_TRADE=false")
                exit_code = 1
            else:
                print(f"[OK] {alias} set")
        for attr, alias in (
            ("polymarket_api_key", "POLYMARKET_API_KEY"),
            ("polymarket_secret", "POLYMARKET_SECRET"),
            ("polymarket_passphrase", "POLYMARKET_PASSPHRASE"),
        ):
            val = getattr(settings, attr, "")
            if _is_blank(val):
                print(f"[--] {alias} empty")
            else:
                print(f"[OK] {alias} set")
        if not _l2_creds_ok(settings):
            print(
                "[X] POLYMARKET_API_KEY / POLYMARKET_SECRET / POLYMARKET_PASSPHRASE: "
                "set all three, or leave all three empty to derive from WALLET_PRIVATE_KEY at runtime"
            )
            exit_code = 1
        elif (
            _is_blank(settings.polymarket_api_key)
            and _is_blank(settings.polymarket_secret)
            and _is_blank(settings.polymarket_passphrase)
        ):
            print("[OK] POLYMARKET_* empty — L2 creds will be derived from WALLET_PRIVATE_KEY at runtime")

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
        ("ARB_CYCLE_ERROR_BACKOFF_SECONDS", settings.arb_cycle_error_backoff_seconds),
        ("GAMMA_HTTP_TIMEOUT_SECONDS", settings.gamma_http_timeout_seconds),
        ("GAMMA_EVENT_PAGE_SIZE", settings.gamma_event_page_size),
        ("GAMMA_EVENT_MAX_PAGES", settings.gamma_event_max_pages),
        ("GAMMA_MARKET_PAGE_SIZE", settings.gamma_market_page_size),
        ("GAMMA_MARKET_MAX_PAGES", settings.gamma_market_max_pages),
        ("MAX_TRACKED_EVENTS", settings.max_tracked_events),
        ("CLOB_BOOK_FETCH_CONCURRENCY", settings.clob_book_fetch_concurrency),
        ("CLOB_BOOK_RETRY_ATTEMPTS", settings.clob_book_retry_attempts),
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
