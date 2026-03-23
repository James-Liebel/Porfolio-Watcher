from __future__ import annotations

import asyncio
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.alerts.telegram import TelegramAlerter  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.risk.manager import RiskManager  # noqa: E402
from src.storage.db import Database  # noqa: E402


def _setup_instructions() -> None:
    print(
        "To set up Telegram:\n"
        " 1. Open Telegram and search @BotFather\n"
        " 2. Send /newbot — follow the prompts\n"
        " 3. Copy the token into TELEGRAM_BOT_TOKEN in .env\n"
        " 4. Search @userinfobot — it shows your chat ID\n"
        " 5. Copy it into TELEGRAM_CHAT_ID in .env\n"
        " 6. Send any message to your new bot first (required before it can message you)"
    )


async def main() -> int:
    cfg = get_settings()
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        _setup_instructions()
        return 1

    db = Database()
    await db.init()
    risk = RiskManager(cfg)
    telegram = TelegramAlerter(cfg)
    telegram.wire(risk, db)

    # Build app without long-running polling.
    from telegram.ext import Application

    app = Application.builder().token(cfg.telegram_bot_token).build()
    telegram._app = app  # use sender utility methods
    await app.initialize()
    try:
        await telegram.send(
            "✅ Polymarket bot Telegram connection confirmed.\nBot is ready for paper trading."
        )

        # Smoke-test command handlers by calling them with a fake update shell.
        class _Msg:
            def __init__(self) -> None:
                self.responses = []

            async def reply_text(self, text: str) -> None:
                self.responses.append(text)

        class _Update:
            def __init__(self) -> None:
                self.message = _Msg()

        update = _Update()
        await telegram._cmd_status(update, None)  # type: ignore[arg-type]
        await telegram._cmd_halt(update, None)  # type: ignore[arg-type]
        await telegram._cmd_resume(update, None)  # type: ignore[arg-type]
        await telegram._cmd_trades(update, None)  # type: ignore[arg-type]

        if len(update.message.responses) < 4:
            print("[X] Telegram command handler simulation returned empty responses")
            return 1

        print("[OK] Telegram connected and commands working")
        return 0
    except Exception as exc:
        print(f"[X] Telegram test failed: {exc}")
        return 1
    finally:
        await app.shutdown()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
