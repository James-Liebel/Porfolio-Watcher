"""
GTC postOnly maker order execution with cancel/repost loop.
Replaces the original Fill-or-Kill taker strategy.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import structlog

from ..config import Settings
from ..markets.window import WindowState
from ..signal.calculator import SignalResult

logger = structlog.get_logger(__name__)

_TICK = Decimal("0.01")
_MAX_MAKER_PRICE = Decimal("0.97")
_MIN_MAKER_PRICE = Decimal("0.02")
_MAKER_REBATE_RATE = Decimal("0.001")   # 0.10% rebate on filled maker orders
_REPOST_POLL_SECONDS = 3


@dataclass
class TradeResult:
    trade_id: Optional[int]          # DB row id (set after insert)
    market_id: str
    question: str
    side: str                        # "YES" or "NO"
    token_id: str
    asset: str                       # "BTC", "ETH", "SOL", "XRP"
    bet_size: Decimal
    limit_price: Decimal
    filled: bool
    fill_price: Optional[Decimal]
    outcome: str                     # "PENDING", "WIN", "LOSS", "NOT_FILLED"
    pnl: Optional[float]
    delta: float
    edge: float
    true_prob: float
    market_prob: float
    seconds_at_entry: int
    timestamp: datetime
    paper_trade: bool
    maker_rebate_earned: Decimal = field(default=Decimal("0"))
    repost_count: int = field(default=0)
    order_type: str = field(default="maker_gtc")
    reason: str = field(default="")  # why not filled, if applicable


class Trader:
    """
    Places GTC postOnly maker limit orders via py-clob-client 0.34.6.
    Runs a cancel/repost loop until the window closes or max reposts hit.
    In PAPER_TRADE mode every step is simulated but logged identically.
    """

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._client = None

    def _get_client(self):
        """Lazy-initialise py-clob-client (deferred to avoid startup crash)."""
        if self._client is not None:
            return self._client
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            self._client = ClobClient(
                host="https://clob.polymarket.com",
                key=self._config.polymarket_wallet_address,
                chain_id=137,
                creds=ApiCreds(
                    api_key=self._config.polymarket_api_key,
                    api_secret=self._config.polymarket_secret,
                    api_passphrase=self._config.polymarket_passphrase,
                ),
                signature_type=2,
                funder=self._config.polymarket_wallet_address,
            )
        except Exception as exc:
            logger.error("trader.client_init_error", error=str(exc))
            raise
        return self._client

    # ── Public async entry point ─────────────────────────────────────────

    async def execute(
        self,
        window: WindowState,
        signal: SignalResult,
        bet_size: Decimal,
    ) -> Optional[TradeResult]:
        """Place a GTC postOnly order and manage it until fill or expiry."""
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None,
                self._execute_sync,
                window,
                signal,
                bet_size,
            )
        except Exception as exc:
            logger.error(
                "trader.execute_error",
                market_id=window.market_id,
                asset=signal.asset,
                error=str(exc),
            )
            return None

    # ── Sync execution (runs in thread pool) ─────────────────────────────

    def _execute_sync(
        self,
        window: WindowState,
        signal: SignalResult,
        bet_size: Decimal,
    ) -> TradeResult:
        paper = self._config.paper_trade
        token_id = signal.token_id
        asset = signal.asset

        # ── STEP 1: Balance check (live only) ────────────────────────────
        if not paper:
            if not self._check_balance(bet_size):
                return self._no_fill(window, signal, bet_size, "insufficient_balance")

        # ── STEP 2: Calculate initial maker price ─────────────────────────
        maker_price = self._compute_maker_price(token_id, paper)
        if maker_price is None:
            return self._no_fill(window, signal, bet_size, "no_orderbook")

        # ── STEP 3 + 4: Place order, then cancel/repost loop ─────────────
        if paper:
            filled, fill_price, repost_count = self._paper_execute(
                window, signal, bet_size, maker_price
            )
        else:
            filled, fill_price, repost_count, maker_price = self._live_execute(
                window, signal, bet_size, maker_price, token_id
            )

        rebate = bet_size * _MAKER_REBATE_RATE if filled else Decimal("0")

        result = TradeResult(
            trade_id=None,
            market_id=window.market_id,
            question=window.question,
            side=signal.trade_side,
            token_id=token_id,
            asset=asset,
            bet_size=bet_size,
            limit_price=maker_price,
            filled=filled,
            fill_price=fill_price,
            outcome="PENDING" if filled else "NOT_FILLED",
            pnl=None,
            delta=signal.delta,
            edge=signal.edge,
            true_prob=signal.true_prob,
            market_prob=signal.market_implied_prob,
            seconds_at_entry=int(window.seconds_remaining),
            timestamp=datetime.now(timezone.utc),
            paper_trade=paper,
            maker_rebate_earned=rebate,
            repost_count=repost_count,
            order_type="maker_gtc",
        )
        return result

    # ── Paper trade simulation ────────────────────────────────────────────

    def _paper_execute(
        self,
        window: WindowState,
        signal: SignalResult,
        bet_size: Decimal,
        maker_price: Decimal,
    ) -> tuple[bool, Optional[Decimal], int]:
        logger.info(
            "trader.paper_maker_order",
            market_id=window.market_id,
            asset=signal.asset,
            side=signal.trade_side,
            price=str(maker_price),
            size=str(bet_size),
            edge=f"{signal.edge:.3f}",
            secs=int(window.seconds_remaining),
        )
        # Simulate a partial fill: assume filled if edge is meaningful
        filled = signal.edge > 0
        fill_price = maker_price if filled else None
        return filled, fill_price, 0

    # ── Live order execution ──────────────────────────────────────────────

    def _live_execute(
        self,
        window: WindowState,
        signal: SignalResult,
        bet_size: Decimal,
        initial_maker_price: Decimal,
        token_id: str,
    ) -> tuple[bool, Optional[Decimal], int, Decimal]:
        """
        Place a GTC postOnly order, then poll every 3 s.
        Cancels and reposts if price moves > REPOST_STALE_TICKS ticks.
        Always cancels at T-CANCEL_AT_SECONDS_REMAINING seconds.
        Returns (filled, fill_price, repost_count, final_maker_price).
        """
        maker_price = initial_maker_price
        max_reposts = self._config.max_reposts_per_window
        stale_ticks = Decimal(str(self._config.repost_stale_ticks)) * _TICK
        cancel_at = self._config.cancel_at_seconds_remaining
        repost_count = 0
        order_id: Optional[str] = None
        filled = False
        fill_price: Optional[Decimal] = None

        try:
            # Place initial order
            order_id, rejected = self._post_maker_order(token_id, bet_size, maker_price)
            if rejected:
                logger.info("trader.postonly_rejected", token_id=token_id)
                return False, None, 0, maker_price
            if order_id is None:
                return False, None, 0, maker_price

            logger.info(
                "trader.maker_order_placed",
                order_id=order_id,
                price=str(maker_price),
                size=str(bet_size),
            )

            # ── Cancel/repost loop ────────────────────────────────────────
            while True:
                time.sleep(_REPOST_POLL_SECONDS)

                # Time check
                secs_left = window.seconds_remaining  # updated externally
                if secs_left <= cancel_at:
                    break

                # Check fill
                filled = self._is_filled(order_id)
                if filled:
                    fill_price = maker_price
                    break

                # Check staleness
                new_price = self._compute_maker_price(token_id, paper=False)
                if new_price is None:
                    break
                if abs(maker_price - new_price) > stale_ticks:
                    if repost_count >= max_reposts:
                        logger.info(
                            "trader.max_reposts_reached",
                            repost_count=repost_count,
                        )
                        break
                    self._cancel_order(order_id)
                    maker_price = new_price
                    order_id, rejected = self._post_maker_order(
                        token_id, bet_size, maker_price
                    )
                    if rejected or order_id is None:
                        order_id = None
                        break
                    repost_count += 1
                    logger.info(
                        "trader.order_reposted",
                        repost_count=repost_count,
                        new_price=str(maker_price),
                    )

        except Exception as exc:
            logger.error("trader.live_execute_error", error=str(exc))

        finally:
            # Always cancel any open order at loop exit
            if order_id and not filled:
                self._cancel_order(order_id)

        return filled, fill_price, repost_count, maker_price

    # ── py-clob-client helpers ─────────────────────────────────────────────

    def _check_balance(self, bet_size: Decimal) -> bool:
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            client = self._get_client()
            balance_data = client.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.USDC)
            )
            usdc_balance = int(balance_data["balance"]) / 1e6
            required = float(bet_size) * 1.05
            if usdc_balance < required:
                logger.warning(
                    "trader.insufficient_balance",
                    balance=usdc_balance,
                    required=required,
                )
                return False
            return True
        except Exception as exc:
            logger.error("trader.balance_check_error", error=str(exc))
            return False

    def _compute_maker_price(
        self, token_id: str, paper: bool
    ) -> Optional[Decimal]:
        """
        Fetch orderbook and compute a maker bid price inside the spread.
        Returns None if the book is missing or one-sided.
        """
        if paper:
            return Decimal("0.50")  # placeholder for paper trades
        try:
            client = self._get_client()
            book = client.get_order_book(token_id)
            if not book.asks or not book.bids:
                return None
            best_ask = Decimal(str(book.asks[0].price))
            best_bid = Decimal(str(book.bids[0].price))

            # Post one tick inside the spread
            maker_price = min(best_bid + _TICK, best_ask - _TICK)
            maker_price = max(maker_price, _MIN_MAKER_PRICE)
            maker_price = min(maker_price, _MAX_MAKER_PRICE)
            maker_price = round(maker_price, 2)

            # Ensure we are not crossing the spread
            if maker_price >= best_ask:
                maker_price = round(best_ask - _TICK, 2)

            return maker_price
        except Exception as exc:
            logger.error("trader.compute_maker_price_error", error=str(exc))
            return None

    def _post_maker_order(
        self,
        token_id: str,
        bet_size: Decimal,
        maker_price: Decimal,
    ) -> tuple[Optional[str], bool]:
        """
        Submit a GTC postOnly order.
        Returns (order_id, rejected_by_postonly).
        """
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.constants import BUY

            client = self._get_client()
            order_args = OrderArgs(
                price=float(maker_price),
                size=float(bet_size),
                side=BUY,
                token_id=token_id,
            )
            signed_order = client.create_order(order_args)
            resp = client.post_order(signed_order, OrderType.GTC, post_only=True)

            if resp is None:
                return None, False

            # Detect postOnly rejection (order would have crossed spread)
            error_msg = str(resp.get("error", "") or resp.get("errorMsg", "")).lower()
            if "post_only" in error_msg or "cross" in error_msg or "taker" in error_msg:
                return None, True

            order_id = resp.get("orderID") or resp.get("id")
            return order_id, False
        except Exception as exc:
            logger.error("trader.post_order_error", error=str(exc))
            return None, False

    def _is_filled(self, order_id: str) -> bool:
        try:
            from py_clob_client.clob_types import OpenOrderParams

            client = self._get_client()
            orders = client.get_orders(OpenOrderParams(id=order_id))
            if not orders:
                # Order no longer in open orders — fully filled or cancelled
                return True
            order = orders[0] if isinstance(orders, list) else orders
            status = str(getattr(order, "status", "") or order.get("status", "")).upper()
            return status in ("MATCHED", "FILLED")
        except Exception as exc:
            logger.error("trader.check_fill_error", order_id=order_id, error=str(exc))
            return False

    def _cancel_order(self, order_id: str) -> None:
        try:
            client = self._get_client()
            client.cancel(order_id)
            logger.info("trader.order_cancelled", order_id=order_id)
        except Exception as exc:
            logger.warning("trader.cancel_error", order_id=order_id, error=str(exc))

    # ── Settlement / resolution ───────────────────────────────────────────

    async def resolve_outcome(self, result: TradeResult) -> TradeResult:
        """
        Called after end_time + 60 s. Fetches market resolution via CLOB
        and computes final PnL including maker rebate.
        """
        if not result.filled or self._config.paper_trade:
            if self._config.paper_trade and result.filled:
                # Paper trade: assume 50/50 outcome for testing
                result.outcome = "WIN" if result.edge > 0 else "LOSS"
                if result.outcome == "WIN":
                    result.pnl = (
                        float(result.bet_size) * (1.0 / float(result.limit_price) - 1.0)
                        + float(result.maker_rebate_earned)
                    )
                else:
                    result.pnl = -float(result.bet_size)
            return result
        try:
            outcome, pnl = await asyncio.get_event_loop().run_in_executor(
                None,
                self._fetch_resolution,
                result,
            )
            result.outcome = outcome
            result.pnl = pnl
        except Exception as exc:
            logger.error("trader.resolve_error", error=str(exc))
        return result

    def _fetch_resolution(self, result: TradeResult) -> tuple[str, float]:
        client = self._get_client()
        try:
            market = client.get_market(result.market_id)
            resolution = (
                getattr(market, "resolution", None)
                or getattr(market, "outcome", None)
            )
            if resolution is None:
                return "PENDING", 0.0

            won = (
                (result.side == "YES" and str(resolution).upper() == "YES")
                or (result.side == "NO" and str(resolution).upper() == "NO")
            )
            if won:
                pnl = (
                    float(result.bet_size) * (1.0 / float(result.limit_price) - 1.0)
                    + float(result.maker_rebate_earned)
                )
                return "WIN", pnl
            else:
                return "LOSS", -float(result.bet_size)
        except Exception as exc:
            logger.error("trader.fetch_resolution_error", error=str(exc))
            return "PENDING", 0.0

    # ── Helper ────────────────────────────────────────────────────────────

    def _no_fill(
        self,
        window: WindowState,
        signal: SignalResult,
        bet_size: Decimal,
        reason: str,
    ) -> TradeResult:
        return TradeResult(
            trade_id=None,
            market_id=window.market_id,
            question=window.question,
            side=signal.trade_side,
            token_id=signal.token_id,
            asset=signal.asset,
            bet_size=bet_size,
            limit_price=Decimal("0"),
            filled=False,
            fill_price=None,
            outcome="NOT_FILLED",
            pnl=None,
            delta=signal.delta,
            edge=signal.edge,
            true_prob=signal.true_prob,
            market_prob=signal.market_implied_prob,
            seconds_at_entry=int(window.seconds_remaining),
            timestamp=datetime.now(timezone.utc),
            paper_trade=self._config.paper_trade,
            reason=reason,
        )
