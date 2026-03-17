from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # ── Polymarket credentials ──────────────────────────────────────────
    # Required for live trading only; leave empty for paper trade mode.
    polymarket_api_key: str = Field(default="", alias="POLYMARKET_API_KEY")
    polymarket_secret: str = Field(default="", alias="POLYMARKET_SECRET")
    polymarket_passphrase: str = Field(default="", alias="POLYMARKET_PASSPHRASE")
    polymarket_wallet_address: str = Field(default="", alias="POLYMARKET_WALLET_ADDRESS")

    # ── Polygon wallet ──────────────────────────────────────────────────
    # Required for live trading only; leave empty for paper trade mode.
    wallet_private_key: str = Field(default="", alias="WALLET_PRIVATE_KEY")

    # ── Polygon RPC ─────────────────────────────────────────────────────
    polygon_rpc_url: str = Field(
        default="https://polygon-rpc.com", alias="POLYGON_RPC_URL"
    )

    # ── Telegram ────────────────────────────────────────────────────────
    # Leave empty to disable Telegram alerts (safe for paper trading).
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    # ── Strategy ────────────────────────────────────────────────────────
    edge_threshold: float = Field(default=0.07, alias="EDGE_THRESHOLD")
    entry_window_seconds: int = Field(default=30, alias="ENTRY_WINDOW_SECONDS")
    min_seconds_remaining: int = Field(default=3, alias="MIN_SECONDS_REMAINING")
    max_bet_fraction: float = Field(default=0.06, alias="MAX_BET_FRACTION")
    kelly_fraction: float = Field(default=0.20, alias="KELLY_FRACTION")
    target_edge_for_max_size: float = Field(
        default=0.12, alias="TARGET_EDGE_FOR_MAX_SIZE"
    )
    min_bet_usd: float = Field(default=1.0, alias="MIN_BET_USD")
    daily_loss_cap: float = Field(default=0.10, alias="DAILY_LOSS_CAP")
    min_market_liquidity: float = Field(default=750.0, alias="MIN_MARKET_LIQUIDITY")
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
    max_reposts_per_window: int = Field(default=4, alias="MAX_REPOSTS_PER_WINDOW")
    repost_stale_ticks: int = Field(default=2, alias="REPOST_STALE_TICKS")
    cancel_at_seconds_remaining: int = Field(default=6, alias="CANCEL_AT_SECONDS_REMAINING")
    max_maker_aggression_ticks: int = Field(
        default=3, alias="MAX_MAKER_AGGRESSION_TICKS"
    )
    maker_rebate_bps_assumption: float = Field(
        default=0.0, alias="MAKER_REBATE_BPS_ASSUMPTION"
    )

    # ── Multi-asset risk ─────────────────────────────────────────────────
    max_positions_per_asset: int = Field(default=1, alias="MAX_POSITIONS_PER_ASSET")
    max_total_exposure_pct: float = Field(default=0.40, alias="MAX_TOTAL_EXPOSURE_PCT")


def get_settings() -> Settings:
    return Settings()
