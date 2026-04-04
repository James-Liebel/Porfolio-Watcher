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
    max_tracked_events: int = Field(
        default=300,
        alias="MAX_TRACKED_EVENTS",
        description="Cap on events kept after sort; CLOB books are fetched only for this many.",
    )
    # Paginated Gamma /events fetch (offset + limit per page).
    gamma_event_page_size: int = Field(default=150, alias="GAMMA_EVENT_PAGE_SIZE")
    gamma_event_max_pages: int = Field(
        default=25,
        alias="GAMMA_EVENT_MAX_PAGES",
        description="Max pages to pull for event metadata (stops early on short/empty page).",
    )
    # Paginated Gamma /markets fetch — drives how many distinct events can be discovered.
    gamma_market_page_size: int = Field(default=500, alias="GAMMA_MARKET_PAGE_SIZE")
    gamma_market_max_pages: int = Field(
        default=40,
        alias="GAMMA_MARKET_MAX_PAGES",
        description="Max pages of markets (page_size * max_pages rows upper bound before dedupe).",
    )
    # aiohttp total timeout for Gamma REST calls (events/markets).
    gamma_http_timeout_seconds: float = Field(
        default=60.0, alias="GAMMA_HTTP_TIMEOUT_SECONDS"
    )
    # Limit parallel CLOB get_order_book calls per cycle to avoid throttling and thread pile-up.
    clob_book_fetch_concurrency: int = Field(
        default=24, alias="CLOB_BOOK_FETCH_CONCURRENCY"
    )
    # Retries when get_order_book raises (e.g. transient network). 0 = single attempt only.
    clob_book_retry_attempts: int = Field(default=2, alias="CLOB_BOOK_RETRY_ATTEMPTS")
    clob_book_retry_delay_seconds: float = Field(
        default=0.35, alias="CLOB_BOOK_RETRY_DELAY_SECONDS"
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
        default=0.22,
        alias="MAX_EVENT_EXPOSURE_PCT",
        description=(
            "Max fraction of max(equity, contributed_capital) deployable in one event "
            "(positions + reserved). Must be ≥ MAX_BASKET_NOTIONAL / bankroll for typical baskets."
        ),
    )
    max_basket_notional: float = Field(default=50.0, alias="MAX_BASKET_NOTIONAL")
    max_total_open_baskets: int = Field(
        default=4, alias="MAX_TOTAL_OPEN_BASKETS"
    )
    max_baskets_per_strategy: int = Field(
        default=2, alias="MAX_BASKETS_PER_STRATEGY"
    )
    max_opportunities_per_cycle: int = Field(
        default=3, alias="MAX_OPPORTUNITIES_PER_CYCLE"
    )
    opportunity_cooldown_seconds: int = Field(
        default=300, alias="OPPORTUNITY_COOLDOWN_SECONDS"
    )
    # 0 = disabled. Max (ask−bid)/mid in bps for each CLOB leg in a structural basket; skips wide/fragile books.
    max_arb_leg_spread_bps: float = Field(
        default=750.0,
        alias="MAX_ARB_LEG_SPREAD_BPS",
    )
    # 0 = disabled. Drop opportunities with expected_profit below this (after fees in the scanner).
    arb_min_expected_profit_usd: float = Field(
        default=0.25,
        alias="ARB_MIN_EXPECTED_PROFIT_USD",
    )
    # 0 = disabled. If a cycle has this many synthetic books, skip new executions (opportunities still logged as rejected).
    arb_halt_execution_if_synthetic_books_ge: int = Field(
        default=6,
        alias="ARB_HALT_EXECUTION_IF_SYNTHETIC_BOOKS_GE",
    )
    # 0 = disabled. Halt if equity falls this fraction below the session peak (mark-to-market). Cleared on resume.
    arb_trailing_equity_drawdown_pct: float = Field(
        default=0.0,
        alias="ARB_TRAILING_EQUITY_DRAWDOWN_PCT",
    )
    # 0 = disabled. Halt if realized PnL vs process start drops by more than this (session = since first init).
    arb_session_realized_loss_usd: float = Field(
        default=0.0,
        alias="ARB_SESSION_REALIZED_LOSS_USD",
    )
    # 0 = disabled. Halt after this many failed basket executions in a row (partial fills / unwind paths).
    arb_consecutive_execution_failures_halt: int = Field(
        default=3,
        alias="ARB_CONSECUTIVE_EXECUTION_FAILURES_HALT",
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
    paper_spread_penalty_bps: float = Field(
        default=15.0,
        alias="PAPER_SPREAD_PENALTY_BPS",
        description=(
            "Extra cost per BUY fill (bps of notional) in paper mode — same in scanner and PaperExchange. "
            "Models residual spread / impact vs the CLOB snapshot; use 0 only for fee-only stress tests."
        ),
    )
    auto_settle_resolved_events: bool = Field(
        default=True, alias="AUTO_SETTLE_RESOLVED_EVENTS"
    )
    replay_output_dir: str = Field(default="data/replays", alias="REPLAY_OUTPUT_DIR")
    category_allowlist: str = Field(default="", alias="CATEGORY_ALLOWLIST")
    category_blocklist: str = Field(default="", alias="CATEGORY_BLOCKLIST")

    # Prefer neg-risk-eligible events when ranking the universe (liquidity tie-breaker).
    universe_prefer_neg_risk: bool = Field(default=True, alias="UNIVERSE_PREFER_NEG_RISK")
    # 0 = disabled. Otherwise keep events whose endDate is within this many hours (upper bound).
    universe_max_hours_to_resolution: float = Field(
        default=0.0, alias="UNIVERSE_MAX_HOURS_TO_RESOLUTION"
    )
    # 0 = disabled. Drop events resolving sooner than this many hours (lower bound).
    universe_min_hours_to_resolution: float = Field(
        default=0.0, alias="UNIVERSE_MIN_HOURS_TO_RESOLUTION"
    )
    # Log per-cycle scanner diagnostics (near-miss edges, structural counts) at INFO.
    arb_log_cycle_diagnostics: bool = Field(default=True, alias="ARB_LOG_CYCLE_DIAGNOSTICS")

    # ── Optional directional overlay (paper): news + crypto momentum vs CLOB ask ──
    enable_directional_overlay: bool = Field(
        default=False,
        alias="ENABLE_DIRECTIONAL_OVERLAY",
        description=(
            "Second sleeve: after structural arb, optionally buy YES on simple binary markets "
            "when blended model probability exceeds ask by DIRECTIONAL_OVERLAY_MIN_EDGE. Paper only."
        ),
    )
    directional_overlay_every_n_cycles: int = Field(
        default=3,
        alias="DIRECTIONAL_OVERLAY_EVERY_N_CYCLES",
        description="Run overlay every N arb cycles (reduces RSS / CoinGecko load).",
    )
    directional_overlay_only_when_no_arb: bool = Field(
        default=True,
        alias="DIRECTIONAL_OVERLAY_ONLY_WHEN_NO_ARB",
        description="If true, overlay runs only when this cycle found zero arb opportunities and executed zero.",
    )
    directional_overlay_max_events_per_cycle: int = Field(
        default=5,
        alias="DIRECTIONAL_OVERLAY_MAX_EVENTS_PER_CYCLE",
    )
    directional_overlay_min_edge: float = Field(
        default=0.10,
        alias="DIRECTIONAL_OVERLAY_MIN_EDGE",
        description="Min (model_p - yes_ask) to consider a paper FOK buy.",
    )
    directional_overlay_max_spread: float = Field(
        default=0.12,
        alias="DIRECTIONAL_OVERLAY_MAX_SPREAD",
        description="Skip YES book if (ask - bid) exceeds this (wide spread = unreliable mid).",
    )
    directional_overlay_max_notional: float = Field(
        default=12.0,
        alias="DIRECTIONAL_OVERLAY_MAX_NOTIONAL",
        description="Cap notional (USD) per overlay fill.",
    )
    directional_overlay_max_contracts: float = Field(
        default=120.0,
        alias="DIRECTIONAL_OVERLAY_MAX_CONTRACTS",
    )
    directional_overlay_min_contracts: float = Field(
        default=1.0,
        alias="DIRECTIONAL_OVERLAY_MIN_CONTRACTS",
    )
    directional_overlay_shrink_weight: float = Field(
        default=0.28,
        alias="DIRECTIONAL_OVERLAY_SHRINK_WEIGHT",
        description="Blend weight on market mid inside predict_history_shrunk for overlay.",
    )
    directional_overlay_cash_floor: float = Field(
        default=75.0,
        alias="DIRECTIONAL_OVERLAY_CASH_FLOOR",
        description="Minimum cash to leave after an overlay trade (safety reserve for arb).",
    )
    directional_overlay_llm_news: bool = Field(
        default=False,
        alias="DIRECTIONAL_OVERLAY_LLM_NEWS",
        description=(
            "If true, blend Ollama / OpenAI-compatible news probability (predict_news_llm) "
            "with the keyword branch; falls back to keywords on LLM errors. Paper overlay only."
        ),
    )

    # ── Storage (multi-agent: one SQLite file per trader process) ─────────
    arb_sqlite_path: str = Field(
        default="",
        alias="ARB_SQLITE_PATH",
        description="If set, legacy DB + arb repository use this file; otherwise data/trades.db.",
    )
    # Shown in /health and dashboard when running multiple local traders.
    agent_display_name: str = Field(default="", alias="AGENT_DISPLAY_NAME")

    # ── Control API ─────────────────────────────────────────────────────
    control_api_port: int = Field(default=8765, alias="CONTROL_API_PORT")
    # Empty = no token auth on loopback API; set a strong random value if the port may be reachable by others.
    control_api_token: str = Field(default="", alias="CONTROL_API_TOKEN")

    # ── Logging ─────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ── Paper trade mode ────────────────────────────────────────────────
    paper_trade: bool = Field(default=True, alias="PAPER_TRADE")
    paper_equity_snapshot_log: bool = Field(
        default=True,
        alias="PAPER_EQUITY_SNAPSHOT_LOG",
        description="When true and PAPER_TRADE, append one JSON line per arb cycle for offline tracking.",
    )
    paper_equity_log_path: str = Field(
        default="data/paper_tracking/equity.jsonl",
        alias="PAPER_EQUITY_LOG_PATH",
        description="Append-only JSONL of cycle snapshots; set empty to disable writes.",
    )

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

    def category_is_allowed(self, category: str) -> bool:
        """Return True if category passes allowlist/blocklist filters."""
        cat = (category or "").strip().lower()
        blocklist = [s.strip().lower() for s in self.category_blocklist.split(",") if s.strip()]
        if any(blocked in cat for blocked in blocklist):
            return False
        allowlist = [s.strip().lower() for s in self.category_allowlist.split(",") if s.strip()]
        if allowlist and not any(allowed in cat for allowed in allowlist):
            return False
        return True

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
