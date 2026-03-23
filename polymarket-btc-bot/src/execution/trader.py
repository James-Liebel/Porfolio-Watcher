"""
GTC postOnly maker order execution with cancel/repost loop.
Replaces the original Fill-or-Kill taker strategy.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Optional

import structlog

from ..config import Settings
from ..markets.window import WindowState
from ..signal.calculator import SignalResult

logger = structlog.get_logger(__name__)

_DEFAULT_TICK = Decimal("0.01")
_MAX_MAKER_PRICE = Decimal("0.97")
_MIN_MAKER_PRICE = Decimal("0.02")
_REPOST_POLL_SECONDS = 3
_SHADOW_POLL_SECONDS = 1.0
_SHARE_QUANTUM = Decimal("0.01")
_COST_QUANTUM = Decimal("0.0001")


@dataclass
class TradeResult:
    trade_id: Optional[int]          # DB row id (set after insert)
    market_id: str
    question: str
    side: str                        # "YES" or "NO"
    token_id: str
    asset: str                       # "BTC", "ETH", "SOL", "XRP"
    bet_size: Decimal                # total stake in USDC
    share_quantity: Decimal
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

            if self._config.paper_trade:
                self._client = ClobClient(
                    host="https://clob.polymarket.com",
                    chain_id=137,
                    signature_type=2,
                )
            else:
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
            if self._config.paper_trade:
                logger.info(
                    "trader.paper_mode_execution",
                    market_id=window.market_id,
                    asset=signal.asset,
                    side=signal.trade_side,
                    intended_stake=str(bet_size),
                )
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
        stake_budget: Decimal,
    ) -> TradeResult:
        paper = self._config.paper_trade
        token_id = signal.token_id
        asset = signal.asset

        # ── STEP 1: Calculate initial maker price and stake geometry ─────
        maker_price = self._compute_maker_price(token_id, signal, window)
        if maker_price is None:
            return self._no_fill(window, signal, Decimal("0"), "no_orderbook")

        share_quantity = self._compute_share_quantity(stake_budget, maker_price)
        if share_quantity <= 0:
            return self._no_fill(window, signal, Decimal("0"), "invalid_order_size")

        order_cost = self._compute_order_cost(share_quantity, maker_price)

        # ── STEP 2: Balance check (live only) ────────────────────────────
        if not paper:
            if not self._check_balance(order_cost):
                return self._no_fill(window, signal, order_cost, "insufficient_balance")

        # ── STEP 3 + 4: Place order, then cancel/repost loop ─────────────
        if paper:
            (
                filled,
                fill_price,
                repost_count,
                maker_price,
                share_quantity,
                order_cost,
            ) = self._paper_execute(
                window,
                signal,
                stake_budget,
                maker_price,
            )
        else:
            (
                filled,
                fill_price,
                repost_count,
                maker_price,
                share_quantity,
                order_cost,
            ) = self._live_execute(
                window,
                signal,
                stake_budget,
                maker_price,
                token_id,
            )

        rebate = self._compute_rebate(order_cost) if filled else Decimal("0")

        result = TradeResult(
            trade_id=None,
            market_id=window.market_id,
            question=window.question,
            side=signal.trade_side,
            token_id=token_id,
            asset=asset,
            bet_size=order_cost,
            share_quantity=share_quantity,
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
        stake_budget: Decimal,
        initial_maker_price: Decimal,
    ) -> tuple[bool, Optional[Decimal], int, Decimal, Decimal, Decimal]:
        """
        Shadow-trade the live maker loop using the public order book.
        Uses the same repost cadence and price logic as live execution.
        """
        token_id = signal.token_id
        maker_price = initial_maker_price
        max_reposts = self._config.max_reposts_per_window
        cancel_at = self._config.cancel_at_seconds_remaining
        repost_count = 0
        filled = False
        fill_price: Optional[Decimal] = None
        share_quantity = self._compute_share_quantity(stake_budget, maker_price)
        order_cost = self._compute_order_cost(share_quantity, maker_price)
        last_requote_check = time.monotonic()
        client = self._get_client()
        book = client.get_order_book(token_id)
        tick_size = self._resolve_tick_size(book, window)
        stale_ticks = Decimal(str(self._config.repost_stale_ticks)) * tick_size
        better_ahead, same_level_ahead = self._estimate_queue_ahead(book, maker_price)
        remaining_own = share_quantity
        last_better_visible = better_ahead
        last_same_visible = same_level_ahead
        seen_trade_keys: set[str] = set()

        logger.info(
            "trader.paper_shadow_order_started",
            market_id=window.market_id,
            price=str(maker_price),
            shares=str(share_quantity),
            cost=str(order_cost),
            queue_ahead=str(better_ahead + same_level_ahead),
        )

        try:
            while True:
                time.sleep(_SHADOW_POLL_SECONDS)

                secs_left = window.seconds_remaining
                if secs_left <= cancel_at:
                    logger.info("trader.paper_shadow_expired", market_id=window.market_id)
                    break

                book = client.get_order_book(token_id)
                if not book.asks or not book.bids:
                    break

                best_ask = Decimal(str(book.asks[0].price))
                tick_size = self._resolve_tick_size(book, window)
                stale_ticks = Decimal(str(self._config.repost_stale_ticks)) * tick_size
                better_visible, same_visible = self._estimate_queue_ahead(
                    book,
                    maker_price,
                )

                if better_visible > last_better_visible:
                    better_ahead += better_visible - last_better_visible
                if better_visible < last_better_visible:
                    better_ahead = max(
                        better_ahead - (last_better_visible - better_visible),
                        Decimal("0"),
                    )
                last_better_visible = better_visible

                if same_visible < last_same_visible:
                    consumed_same = min(
                        same_level_ahead,
                        last_same_visible - same_visible,
                    )
                    same_level_ahead = max(same_level_ahead - consumed_same, Decimal("0"))
                last_same_visible = same_visible

                if best_ask <= maker_price:
                    filled = True
                    remaining_own = Decimal("0")

                if not filled:
                    try:
                        trades = client.get_trades(token_id=token_id)
                        traded_volume = self._consume_recent_fill_volume(
                            trades,
                            maker_price,
                            seen_trade_keys,
                        )
                        if traded_volume > 0:
                            if better_ahead > 0:
                                consume_better = min(better_ahead, traded_volume)
                                better_ahead -= consume_better
                                traded_volume -= consume_better
                            if traded_volume > 0 and same_level_ahead > 0:
                                consume_same = min(same_level_ahead, traded_volume)
                                same_level_ahead -= consume_same
                                traded_volume -= consume_same
                            if traded_volume > 0 and better_ahead <= 0 and same_level_ahead <= 0:
                                remaining_own = max(remaining_own - traded_volume, Decimal("0"))
                    except Exception:
                        pass

                if remaining_own <= 0:
                    filled = True

                if filled:
                    fill_price = maker_price
                    order_cost = self._compute_order_cost(share_quantity, fill_price)
                    logger.info(
                        "trader.paper_shadow_filled",
                        market_id=window.market_id,
                        price=str(fill_price),
                        shares=str(share_quantity),
                        cost=str(order_cost),
                    )
                    break

                if (time.monotonic() - last_requote_check) < _REPOST_POLL_SECONDS:
                    continue
                last_requote_check = time.monotonic()

                ideal_price = self._compute_maker_price(token_id, signal, window)
                if ideal_price is None:
                    break

                if abs(maker_price - ideal_price) > stale_ticks:
                    if repost_count >= max_reposts:
                        logger.info(
                            "trader.paper_shadow_max_reposts",
                            market_id=window.market_id,
                        )
                        break

                    maker_price = ideal_price
                    share_quantity = self._compute_share_quantity(stake_budget, maker_price)
                    order_cost = self._compute_order_cost(share_quantity, maker_price)
                    if share_quantity <= 0:
                        break
                    book = client.get_order_book(token_id)
                    tick_size = self._resolve_tick_size(book, window)
                    stale_ticks = Decimal(str(self._config.repost_stale_ticks)) * tick_size
                    better_ahead, same_level_ahead = self._estimate_queue_ahead(
                        book,
                        maker_price,
                    )
                    last_better_visible = better_ahead
                    last_same_visible = same_level_ahead
                    remaining_own = share_quantity
                    seen_trade_keys.clear()
                    repost_count += 1
                    logger.info(
                        "trader.paper_shadow_reposted",
                        market_id=window.market_id,
                        new_price=str(maker_price),
                        reposts=repost_count,
                        shares=str(share_quantity),
                        cost=str(order_cost),
                        queue_ahead=str(better_ahead + same_level_ahead),
                    )

        except Exception as exc:
            logger.error("trader.paper_shadow_error", error=str(exc))

        return filled, fill_price, repost_count, maker_price, share_quantity, order_cost

    # ── Live order execution ──────────────────────────────────────────────

    def _live_execute(
        self,
        window: WindowState,
        signal: SignalResult,
        stake_budget: Decimal,
        initial_maker_price: Decimal,
        token_id: str,
    ) -> tuple[bool, Optional[Decimal], int, Decimal, Decimal, Decimal]:
        """
        Place a GTC postOnly order, then poll every 3 s.
        Cancels and reposts if price moves > REPOST_STALE_TICKS ticks.
        Always cancels at T-CANCEL_AT_SECONDS_REMAINING seconds.
        Returns fill state plus the final maker price, shares, and cost.
        """
        maker_price = initial_maker_price
        max_reposts = self._config.max_reposts_per_window
        cancel_at = self._config.cancel_at_seconds_remaining
        repost_count = 0
        order_id: Optional[str] = None
        filled = False
        fill_price: Optional[Decimal] = None
        share_quantity = self._compute_share_quantity(stake_budget, maker_price)
        order_cost = self._compute_order_cost(share_quantity, maker_price)
        stale_ticks = Decimal(str(self._config.repost_stale_ticks)) * (
            window.minimum_tick_size if window.minimum_tick_size > 0 else _DEFAULT_TICK
        )

        try:
            # Place initial order
            order_id, rejected = self._post_maker_order(
                token_id,
                share_quantity,
                maker_price,
            )
            if rejected:
                logger.info("trader.postonly_rejected", token_id=token_id)
                return False, None, 0, maker_price, share_quantity, order_cost
            if order_id is None:
                return False, None, 0, maker_price, share_quantity, order_cost

            logger.info(
                "trader.maker_order_placed",
                order_id=order_id,
                price=str(maker_price),
                shares=str(share_quantity),
                cost=str(order_cost),
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
                    order_cost = self._compute_order_cost(share_quantity, fill_price)
                    break

                # Check staleness
                new_price = self._compute_maker_price(token_id, signal, window)
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
                    share_quantity = self._compute_share_quantity(stake_budget, maker_price)
                    order_cost = self._compute_order_cost(share_quantity, maker_price)
                    if share_quantity <= 0:
                        break
                    order_id, rejected = self._post_maker_order(
                        token_id,
                        share_quantity,
                        maker_price,
                    )
                    if rejected or order_id is None:
                        order_id = None
                        break
                    repost_count += 1
                    logger.info(
                        "trader.order_reposted",
                        repost_count=repost_count,
                        new_price=str(maker_price),
                        shares=str(share_quantity),
                        cost=str(order_cost),
                    )

        except Exception as exc:
            logger.error("trader.live_execute_error", error=str(exc))

        finally:
            # Always cancel any open order at loop exit
            if order_id and not filled:
                self._cancel_order(order_id)

        return filled, fill_price, repost_count, maker_price, share_quantity, order_cost

    # ── py-clob-client helpers ─────────────────────────────────────────────

    def _check_balance(self, bet_size: Decimal) -> bool:
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            client = self._get_client()
            balance_data = client.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.USDC)
            )
            usdc_balance = int(balance_data["balance"]) / 1e6
            required = float(bet_size) * 1.01
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
        self,
        token_id: str,
        signal: SignalResult,
        window: WindowState,
    ) -> Optional[Decimal]:
        """
        Fetch the order book and place a maker bid inside the spread.
        Quote aggression increases with edge strength and time urgency but
        never crosses the best ask.
        """
        try:
            client = self._get_client()
            book = client.get_order_book(token_id)
            if not book.asks or not book.bids:
                return None
            best_ask = Decimal(str(book.asks[0].price))
            best_bid = Decimal(str(book.bids[0].price))
            tick_size = self._resolve_tick_size(book, window)

            spread = best_ask - best_bid
            if spread <= 0:
                maker_price = best_ask - tick_size
            else:
                spread_ticks = int(
                    (spread / tick_size).to_integral_value(rounding=ROUND_DOWN)
                )
                edge_floor = max(self._config.edge_threshold, 0.01)
                target_edge = max(
                    self._config.target_edge_for_max_size,
                    edge_floor + 0.01,
                )
                edge_progress = self._clamp(
                    (signal.edge - edge_floor) / (target_edge - edge_floor),
                    0.0,
                    1.0,
                )
                urgency = self._clamp(
                    (
                        float(self._config.entry_window_seconds)
                        - self._clamp(
                            float(window.seconds_remaining),
                            float(self._config.cancel_at_seconds_remaining),
                            float(self._config.entry_window_seconds),
                        )
                    )
                    / max(
                        float(
                            self._config.entry_window_seconds
                            - self._config.cancel_at_seconds_remaining
                        ),
                        1.0,
                    ),
                    0.0,
                    1.0,
                )

                max_inside_ticks = max(1, min(self._config.max_maker_aggression_ticks, max(spread_ticks - 1, 1)))
                desired_ticks = 1 + int(
                    (max_inside_ticks - 1) * (0.55 * edge_progress + 0.45 * urgency)
                )
                maker_price = best_bid + (tick_size * max(1, desired_ticks))

            maker_price = max(maker_price, _MIN_MAKER_PRICE)
            maker_price = min(maker_price, _MAX_MAKER_PRICE)
            maker_price = self._quantize_to_tick(maker_price, tick_size)

            if maker_price >= best_ask:
                maker_price = self._quantize_to_tick(best_ask - tick_size, tick_size)

            if maker_price < _MIN_MAKER_PRICE or maker_price <= 0:
                return None
            return maker_price
        except Exception as exc:
            logger.error("trader.compute_maker_price_error", error=str(exc))
            return None

    def _resolve_tick_size(self, book, window: WindowState) -> Decimal:
        tick = getattr(book, "tick_size", None)
        if tick is None:
            tick = getattr(book, "tickSize", None)
        try:
            resolved = Decimal(str(tick)) if tick is not None else window.minimum_tick_size
        except Exception:
            resolved = window.minimum_tick_size
        if resolved <= 0:
            resolved = window.minimum_tick_size
        if resolved <= 0:
            resolved = _DEFAULT_TICK
        return resolved

    def _quantize_to_tick(self, value: Decimal, tick_size: Decimal) -> Decimal:
        ticks = (value / tick_size).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return (ticks * tick_size).quantize(tick_size, rounding=ROUND_HALF_UP)

    def _estimate_queue_ahead(
        self,
        book,
        maker_price: Decimal,
    ) -> tuple[Decimal, Decimal]:
        better = Decimal("0")
        same = Decimal("0")
        for level in getattr(book, "bids", []) or []:
            price = Decimal(str(level.price))
            size = self._extract_level_size(level)
            if price > maker_price:
                better += size
            elif price == maker_price:
                same += size
        return better, same

    def _extract_level_size(self, level) -> Decimal:
        for attr in ("size", "amount", "quantity"):
            raw = getattr(level, attr, None)
            if raw is None and isinstance(level, dict):
                raw = level.get(attr)
            if raw is not None:
                try:
                    return Decimal(str(raw))
                except Exception:
                    return Decimal("0")
        return Decimal("0")

    def _consume_recent_fill_volume(
        self,
        trades,
        maker_price: Decimal,
        seen_trade_keys: set[str],
    ) -> Decimal:
        fill_volume = Decimal("0")
        for trade in trades or []:
            trade_key = self._trade_key(trade)
            if trade_key in seen_trade_keys:
                continue
            seen_trade_keys.add(trade_key)
            trade_price = self._trade_price(trade)
            if trade_price is None or trade_price > maker_price:
                continue
            fill_volume += self._trade_size(trade)
        return fill_volume

    def _trade_key(self, trade) -> str:
        if isinstance(trade, dict):
            for key in ("id", "tradeID", "transactionHash", "timestamp", "t"):
                if key in trade:
                    return str(trade[key])
            return repr(sorted(trade.items()))
        return repr(trade)

    def _trade_price(self, trade) -> Optional[Decimal]:
        raw = trade.get("price") if isinstance(trade, dict) else getattr(trade, "price", None)
        if raw is None:
            return None
        try:
            return Decimal(str(raw))
        except Exception:
            return None

    def _trade_size(self, trade) -> Decimal:
        if isinstance(trade, dict):
            for key in ("size", "amount", "quantity"):
                if key in trade:
                    try:
                        return Decimal(str(trade[key]))
                    except Exception:
                        return Decimal("0")
        for attr in ("size", "amount", "quantity"):
            raw = getattr(trade, attr, None)
            if raw is not None:
                try:
                    return Decimal(str(raw))
                except Exception:
                    return Decimal("0")
        return Decimal("0")

    def _compute_share_quantity(
        self,
        stake_budget: Decimal,
        price: Decimal,
    ) -> Decimal:
        if stake_budget <= 0 or price <= 0:
            return Decimal("0")
        shares = (stake_budget / price).quantize(_SHARE_QUANTUM, rounding=ROUND_DOWN)
        return max(shares, Decimal("0"))

    def _compute_order_cost(self, share_quantity: Decimal, price: Decimal) -> Decimal:
        if share_quantity <= 0 or price <= 0:
            return Decimal("0")
        return (share_quantity * price).quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP)

    def _compute_rebate(self, order_cost: Decimal) -> Decimal:
        assumed_rate = Decimal(str(self._config.maker_rebate_bps_assumption)) / Decimal("10000")
        return (order_cost * assumed_rate).quantize(
            _COST_QUANTUM,
            rounding=ROUND_HALF_UP,
        )

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

    def _post_maker_order(
        self,
        token_id: str,
        share_quantity: Decimal,
        maker_price: Decimal,
    ) -> tuple[Optional[str], bool]:
        """
        Submit a GTC postOnly order.
        Returns (order_id, rejected_by_postonly).
        """
        try:
            if self._config.paper_trade:
                logger.error(
                    "SAFETY: real order blocked in paper mode",
                    token_id=token_id,
                )
                return None, False
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.constants import BUY

            client = self._get_client()
            order_args = OrderArgs(
                price=float(maker_price),
                size=float(share_quantity),
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

    async def cancel_order(self, order_id: str) -> None:
        await asyncio.get_event_loop().run_in_executor(None, self._cancel_order, order_id)

    # ── Settlement / resolution ───────────────────────────────────────────

    async def resolve_outcome(self, result: TradeResult) -> TradeResult:
        """
        Called after end_time + 60 s. Fetches actual market resolution via CLOB.
        Now realistic: paper trades fetch real results instead of simulation.
        """
        if not result.filled:
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
                    float(result.share_quantity) * (1.0 - float(result.limit_price))
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
            share_quantity=Decimal("0"),
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
