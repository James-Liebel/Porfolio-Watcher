from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ASSET_FLAG_FIELDS = (
    ("BTC", "trade_btc"),
    ("ETH", "trade_eth"),
    ("SOL", "trade_sol"),
    ("XRP", "trade_xrp"),
    ("ADA", "trade_ada"),
    ("DOGE", "trade_doge"),
    ("AVAX", "trade_avax"),
    ("LINK", "trade_link"),
)


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
    max_bet_fraction: float = Field(default=0.03, alias="MAX_BET_FRACTION")
    kelly_fraction: float = Field(default=0.10, alias="KELLY_FRACTION")
    target_edge_for_max_size: float = Field(
        default=0.14, alias="TARGET_EDGE_FOR_MAX_SIZE"
    )
    min_bet_usd: float = Field(default=1.0, alias="MIN_BET_USD")
    daily_loss_cap: float = Field(default=0.05, alias="DAILY_LOSS_CAP")
    min_market_liquidity: float = Field(default=1500.0, alias="MIN_MARKET_LIQUIDITY")
    max_concurrent_positions: int = Field(default=2, alias="MAX_CONCURRENT_POSITIONS")
    initial_bankroll: float = Field(default=300.0, alias="INITIAL_BANKROLL")

    # ── Structural-arb runtime ───────────────────────────────────────────
    gamma_base_url: str = Field(
        default="https://gamma-api.polymarket.com", alias="GAMMA_BASE_URL"
    )
    clob_host: str = Field(
        default="https://clob.polymarket.com", alias="CLOB_HOST"
    )
    arb_poll_seconds: int = Field(default=20, alias="ARB_POLL_SECONDS")
    # Extra delay after a failed run_cycle() before the normal poll wait (reduces tight error loops).
    arb_cycle_error_backoff_seconds: int = Field(
        default=8, alias="ARB_CYCLE_ERROR_BACKOFF_SECONDS"
    )
    max_tracked_events: int = Field(default=100, alias="MAX_TRACKED_EVENTS")
    # aiohttp total timeout for Gamma REST calls (events/markets).
    gamma_http_timeout_seconds: float = Field(
        default=30.0, alias="GAMMA_HTTP_TIMEOUT_SECONDS"
    )
    # Limit parallel CLOB get_order_book calls per cycle to avoid throttling and thread pile-up.
    clob_book_fetch_concurrency: int = Field(
        default=12, alias="CLOB_BOOK_FETCH_CONCURRENCY"
    )
    min_event_liquidity: float = Field(default=2000.0, alias="MIN_EVENT_LIQUIDITY")
    min_outcomes_per_event: int = Field(default=2, alias="MIN_OUTCOMES_PER_EVENT")
    min_complete_set_edge_bps: float = Field(
        default=25.0, alias="MIN_COMPLETE_SET_EDGE_BPS"
    )
    min_neg_risk_edge_bps: float = Field(
        default=40.0, alias="MIN_NEG_RISK_EDGE_BPS"
    )
    max_event_exposure_pct: float = Field(
        default=0.10, alias="MAX_EVENT_EXPOSURE_PCT"
    )
    max_basket_notional: float = Field(default=50.0, alias="MAX_BASKET_NOTIONAL")
    max_total_open_baskets: int = Field(
        default=4, alias="MAX_TOTAL_OPEN_BASKETS"
    )
    max_opportunities_per_cycle: int = Field(
        default=3, alias="MAX_OPPORTUNITIES_PER_CYCLE"
    )
    opportunity_cooldown_seconds: int = Field(
        default=300, alias="OPPORTUNITY_COOLDOWN_SECONDS"
    )
    allow_taker_execution: bool = Field(
        default=True, alias="ALLOW_TAKER_EXECUTION"
    )
    paper_taker_fee_bps: float = Field(
        default=50.0,
        alias="PAPER_TAKER_FEE_BPS",
        description="Taker fee assumption (bps) for paper fills when market fees_enabled; tune to match Polymarket.",
    )
    paper_maker_rebate_bps: float = Field(
        default=0.0, alias="PAPER_MAKER_REBATE_BPS"
    )
    auto_settle_resolved_events: bool = Field(
        default=True, alias="AUTO_SETTLE_RESOLVED_EVENTS"
    )
    replay_output_dir: str = Field(default="data/replays", alias="REPLAY_OUTPUT_DIR")

    # ── Control API ─────────────────────────────────────────────────────
    control_api_port: int = Field(default=8765, alias="CONTROL_API_PORT")
    # Empty = no token auth on loopback API; set a strong random value if the port may be reachable by others.
    control_api_token: str = Field(default="", alias="CONTROL_API_TOKEN")

    # ── Logging ─────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ── Paper trade mode ────────────────────────────────────────────────
    paper_trade: bool = Field(default=True, alias="PAPER_TRADE")

    # ── Portfolio profile ───────────────────────────────────────────────
    strategy_profile: str = Field(default="conservative", alias="STRATEGY_PROFILE")
    auto_asset_selection: bool = Field(default=True, alias="AUTO_ASSET_SELECTION")

    # ── Multi-asset on/off switches ─────────────────────────────────────
    trade_btc: bool = Field(default=True, alias="TRADE_BTC")
    trade_eth: bool = Field(default=True, alias="TRADE_ETH")
    trade_sol: bool = Field(default=True, alias="TRADE_SOL")
    trade_xrp: bool = Field(default=True, alias="TRADE_XRP")
    trade_ada: bool = Field(default=False, alias="TRADE_ADA")
    trade_doge: bool = Field(default=False, alias="TRADE_DOGE")
    trade_avax: bool = Field(default=False, alias="TRADE_AVAX")
    trade_link: bool = Field(default=False, alias="TRADE_LINK")

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

    def manual_asset_flags(self) -> dict[str, bool]:
        return {
            asset: bool(getattr(self, field_name))
            for asset, field_name in _ASSET_FLAG_FIELDS
        }

    def enabled_assets(self) -> tuple[str, ...]:
        flags = self.manual_asset_flags()
        if not self.auto_asset_selection:
            return tuple(asset for asset, enabled in flags.items() if enabled)

        bankroll = max(float(self.initial_bankroll), 0.0)
        profile = (self.strategy_profile or "conservative").strip().lower()

        if profile == "aggressive":
            allowed = set(flags)
        elif profile == "balanced":
            if bankroll < 500:
                allowed = {"BTC", "ETH", "SOL"}
            elif bankroll < 1000:
                allowed = {"BTC", "ETH", "SOL", "XRP"}
            else:
                allowed = set(flags)
        else:
            if bankroll < 500:
                allowed = {"BTC", "ETH"}
            elif bankroll < 1000:
                allowed = {"BTC", "ETH", "SOL"}
            else:
                allowed = {"BTC", "ETH", "SOL", "XRP"}

        return tuple(asset for asset, enabled in flags.items() if enabled and asset in allowed)

    def is_asset_enabled(self, asset: str) -> bool:
        return asset.upper() in set(self.enabled_assets())


def get_settings() -> Settings:
    return Settings()
