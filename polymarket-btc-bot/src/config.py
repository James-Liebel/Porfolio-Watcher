from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Polymarket credentials ──────────────────────────────────────────
    polymarket_api_key: str = Field(..., alias="POLYMARKET_API_KEY")
    polymarket_secret: str = Field(..., alias="POLYMARKET_SECRET")
    polymarket_passphrase: str = Field(..., alias="POLYMARKET_PASSPHRASE")
    polymarket_wallet_address: str = Field(..., alias="POLYMARKET_WALLET_ADDRESS")

    # ── Polygon wallet ──────────────────────────────────────────────────
    wallet_private_key: str = Field(..., alias="WALLET_PRIVATE_KEY")

    # ── Polygon RPC ─────────────────────────────────────────────────────
    polygon_rpc_url: str = Field(
        default="https://polygon-rpc.com", alias="POLYGON_RPC_URL"
    )

    # ── Telegram ────────────────────────────────────────────────────────
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(..., alias="TELEGRAM_CHAT_ID")

    # ── Strategy ────────────────────────────────────────────────────────
    edge_threshold: float = Field(default=0.07, alias="EDGE_THRESHOLD")
    entry_window_seconds: int = Field(default=30, alias="ENTRY_WINDOW_SECONDS")
    min_seconds_remaining: int = Field(default=3, alias="MIN_SECONDS_REMAINING")
    max_bet_fraction: float = Field(default=0.10, alias="MAX_BET_FRACTION")
    kelly_fraction: float = Field(default=0.25, alias="KELLY_FRACTION")
    daily_loss_cap: float = Field(default=0.15, alias="DAILY_LOSS_CAP")
    min_market_liquidity: float = Field(default=500.0, alias="MIN_MARKET_LIQUIDITY")
    max_concurrent_positions: int = Field(default=3, alias="MAX_CONCURRENT_POSITIONS")
    initial_bankroll: float = Field(default=300.0, alias="INITIAL_BANKROLL")

    # ── Control API ─────────────────────────────────────────────────────
    control_api_port: int = Field(default=8765, alias="CONTROL_API_PORT")

    # ── Logging ─────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ── Paper trade mode ────────────────────────────────────────────────
    paper_trade: bool = Field(default=True, alias="PAPER_TRADE")

    # ── Multi-asset on/off switches ─────────────────────────────────────
    trade_btc: bool = Field(default=True, alias="TRADE_BTC")
    trade_eth: bool = Field(default=True, alias="TRADE_ETH")
    trade_sol: bool = Field(default=True, alias="TRADE_SOL")
    trade_xrp: bool = Field(default=True, alias="TRADE_XRP")

    # ── Maker order tuning ───────────────────────────────────────────────
    max_reposts_per_window: int = Field(default=3, alias="MAX_REPOSTS_PER_WINDOW")
    repost_stale_ticks: int = Field(default=2, alias="REPOST_STALE_TICKS")
    cancel_at_seconds_remaining: int = Field(default=5, alias="CANCEL_AT_SECONDS_REMAINING")

    # ── Multi-asset risk ─────────────────────────────────────────────────
    max_positions_per_asset: int = Field(default=1, alias="MAX_POSITIONS_PER_ASSET")
    max_total_exposure_pct: float = Field(default=0.40, alias="MAX_TOTAL_EXPOSURE_PCT")


def get_settings() -> Settings:
    return Settings()
