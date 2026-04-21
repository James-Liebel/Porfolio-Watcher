from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import structlog

from ..config import Settings
from ..storage.db import Database
from .exchange import PaperExchange
from .fees import taker_fee_on_notional
from .market_data import ClobMarketDataService
from .models import (
    ArbEvent,
    ArbOpportunity,
    BasketRecord,
    OrderIntent,
    OrderRecord,
    OutcomeMarket,
    PositionRecord,
    TokenBook,
    utc_now,
)
from .pricing import OpportunityScanner
from .repository import ArbRepository
from .risk import ArbRiskManager
from .universe import GammaUniverseService

logger = structlog.get_logger(__name__)


def _make_exchange(config: Settings) -> PaperExchange:
    if config.arb_live_execution:
        from .live_exchange import LiveClobExchange

        return LiveClobExchange(config)
    return PaperExchange(config)


def _book_source_counts(books: dict[str, TokenBook]) -> dict[str, int]:
    clob = synthetic = other = 0
    for book in books.values():
        src = (book.source or "").strip().lower()
        if src == "clob":
            clob += 1
        elif src == "synthetic":
            synthetic += 1
        else:
            other += 1
    return {
        "books_clob": clob,
        "books_synthetic": synthetic,
        "books_other": other,
    }


class ArbEngine:
    def __init__(
        self,
        config: Settings,
        legacy_db: Database,
        repository: ArbRepository,
        universe: GammaUniverseService | None = None,
        market_data: ClobMarketDataService | None = None,
        scanner: OpportunityScanner | None = None,
        risk: ArbRiskManager | None = None,
        exchange: PaperExchange | None = None,
    ) -> None:
        self._config = config
        self._legacy_db = legacy_db
        self._repository = repository
        self._universe = universe or GammaUniverseService(config)
        self._market_data = market_data or ClobMarketDataService(config)
        self._scanner = scanner or OpportunityScanner(config)
        self._risk = risk or ArbRiskManager(config)
        self._exchange = exchange or _make_exchange(config)
        self._events: dict[str, ArbEvent] = {}
        self._active_event_ids: set[str] = set()
        self._last_books = {}
        self._opportunities: list[ArbOpportunity] = []
        self._baskets: dict[str, BasketRecord] = {}
        self._last_auto_settlements: list[dict[str, Any]] = []
        self._last_cycle_at: datetime | None = None
        self._last_cycle_summary: dict[str, Any] = {}
        self._cycle_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._initialized = False
        self._current_cycle_pct: float | None = 0.0
        self._current_cycle_step = "idle"
        self._complete_set_hold_log_mono: dict[str, float] = {}
        self._last_basket_cash_limit_log_mono: float = 0.0
        self._adaptive_event_target = int(config.max_tracked_events)

    @property
    def risk(self) -> ArbRiskManager:
        return self._risk

    @property
    def exchange(self) -> PaperExchange:
        return self._exchange

    async def initialize(self) -> None:
        if self._initialized:
            return
        if self._config.arb_live_execution:
            if self._config.paper_trade:
                raise ValueError(
                    "ARB_LIVE_EXECUTION requires PAPER_TRADE=false (live CLOB orders are incompatible with paper mode)"
                )
            if not self._config.allow_taker_execution:
                raise ValueError("ARB_LIVE_EXECUTION requires ALLOW_TAKER_EXECUTION=true (FOK taker legs)")
            if self._config.paper_spread_penalty_bps > 0:
                logger.warning(
                    "arb_engine.live_with_paper_spread_penalty",
                    paper_spread_penalty_bps=self._config.paper_spread_penalty_bps,
                    note="Complete-set scanner edges include this penalty; use PAPER_SPREAD_PENALTY_BPS=0 for live.",
                )
        await self._legacy_db.init()
        await self._repository.init()
        await self._risk.hydrate_from_db(self._legacy_db, self._exchange)
        runtime_state = await self._repository.load_runtime_state()
        positions = await self._repository.load_positions()
        if runtime_state:
            self._exchange.restore_state(runtime_state, positions)
        elif positions:
            self._exchange.restore_state(
                {
                    "cash": 0.0,
                    "contributed_capital": 0.0,
                    "realized_pnl": 0.0,
                    "fees_paid": 0.0,
                    "rebates_earned": 0.0,
                },
                positions,
            )
            logger.warning(
                "arb_engine.init_positions_without_runtime_state",
                positions=len(positions),
                note="Restored positions only; arb_runtime_state row missing — cash/PnL baseline may be wrong until next trade.",
            )
        self._baskets = {
            basket.basket_id: basket
            for basket in await self._repository.load_active_baskets()
        }
        self._risk.cooldowns = await self._repository.load_cooldowns()
        await self._repository.replace_positions(self._exchange.get_positions())
        await self._maybe_sync_clob_collateral()
        await self._maybe_sync_live_positions_from_trades()
        await self._repository.replace_positions(self._exchange.get_positions())
        await self._reconcile_nominal_contributed_capital()
        await self._persist_runtime_state()
        self._risk.capture_session_baseline(self._exchange)
        self._initialized = True

    async def _reconcile_nominal_contributed_capital(self) -> None:
        """Set `contributed_capital` from config + deposits table — not stale `arb_runtime_state`.

        Persisted snapshots could be inflated by older live CLOB sync logic or drift; this is the
        canonical "capital committed" number for risk display and drawdown vs INITIAL_BANKROLL.
        """
        total_dep = await self._legacy_db.get_total_deposits()
        nominal = float(self._config.initial_bankroll) + float(total_dep)
        self._exchange.contributed_capital = nominal

    async def _maybe_sync_clob_collateral(self) -> None:
        """Live: refresh ledger cash from CLOB collateral API (deposits / redeems on Polymarket.com)."""
        if self._config.paper_trade or not self._config.arb_sync_clob_collateral_each_cycle:
            return
        if not self._config.arb_live_execution:
            return
        from .live_exchange import LiveClobExchange

        if not isinstance(self._exchange, LiveClobExchange):
            return
        try:
            await asyncio.to_thread(self._exchange.sync_cash_from_clob_collateral)
        except Exception as exc:
            logger.warning("arb_engine.clob_collateral_sync_failed", error=str(exc))

    async def _maybe_sync_live_positions_from_trades(self) -> None:
        """Live: hydrate positions from CLOB trade history so equity includes pre-existing holdings."""
        if self._config.paper_trade or not self._config.arb_live_execution:
            return
        from .live_exchange import LiveClobExchange

        if not isinstance(self._exchange, LiveClobExchange):
            return
        try:
            await asyncio.to_thread(self._exchange.sync_positions_from_clob_trades)
        except Exception as exc:
            logger.warning("arb_engine.live_positions_sync_failed", error=str(exc))

    async def refresh_clob_for_summary_if_stale(self) -> None:
        """Refresh CLOB balance before /summary when data is older than arb_summary_clob_stale_seconds."""
        stale_sec = float(self._config.arb_summary_clob_stale_seconds)
        if stale_sec <= 0:
            return
        if self._config.paper_trade or not self._config.arb_sync_clob_collateral_each_cycle:
            return
        if not self._config.arb_live_execution:
            return
        from .live_exchange import LiveClobExchange

        if not isinstance(self._exchange, LiveClobExchange):
            return
        ex = self._exchange
        now = time.monotonic()
        last = float(getattr(ex, "_last_clob_refresh_mono", 0) or 0)
        if last > 0 and (now - last) < stale_sec:
            return
        try:
            await asyncio.to_thread(ex.sync_cash_from_clob_collateral)
        except Exception as exc:
            logger.warning("arb_engine.summary_clob_refresh_failed", error=str(exc))

    def _equity_bankroll_for_sizing(self) -> float:
        """Capital base for per-basket fraction sizing (grows with redeems after CLOB sync)."""
        ex = self._exchange
        return max(
            float(ex.equity),
            float(ex.contributed_capital),
            float(ex.available_cash),
            1.0,
        )

    def _effective_base_max_basket_notional(self) -> float:
        """
        Per-basket notional cap for scanning/risk before qualified multiplier.
        With ARB_BASKET_NOTIONAL_FRACTION_OF_EQUITY > 0: min(bankroll × fraction, MAX_BASKET_NOTIONAL),
        floored at ARB_BASKET_NOTIONAL_MIN_USD.
        """
        ceiling = float(self._config.max_basket_notional)
        frac = float(self._config.arb_basket_notional_fraction_of_equity)
        if frac <= 1e-12:
            return ceiling
        bankroll = self._equity_bankroll_for_sizing()
        target = bankroll * frac
        scaled = min(target, ceiling)
        floor_usd = max(0.0, float(self._config.arb_basket_notional_min_usd))
        return max(floor_usd, scaled)

    def _spendable_cash_ceiling_for_baskets(self) -> float | None:
        """Max basket notional implied by free USDC; None when cash clamp is disabled."""
        if not self._config.arb_cap_scan_notional_to_available_cash:
            return None
        buf = max(0.0, float(self._config.arb_available_cash_sizing_buffer_usd))
        return max(0.0, float(self._exchange.available_cash) - buf)

    def _clamp_basket_notional_to_available_cash(self, notional: float) -> float:
        cap = self._spendable_cash_ceiling_for_baskets()
        if cap is None:
            return float(notional)
        return min(float(notional), cap)

    async def shutdown(self) -> None:
        self._stop.set()
        await self._universe.close()

    async def run(self) -> None:
        await self.initialize()
        while not self._stop.is_set():
            cycle_failed = False
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                cycle_failed = True
                logger.error("arb_engine.cycle_error", error=str(exc), exc_info=True)
            if cycle_failed:
                backoff = max(1, min(self._config.arb_cycle_error_backoff_seconds, 300))
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._config.arb_poll_seconds)
            except asyncio.TimeoutError:
                continue

    async def run_cycle(self) -> dict[str, Any]:
        await self.initialize()
        async with self._cycle_lock:
            cycle_t0 = time.monotonic()
            # UI/API: do not leave cycle_step as "idle" during Gamma + SQLite work (can be minutes).
            self._current_cycle_step = "loading_universe"
            self._current_cycle_pct = None
            await self._maybe_sync_clob_collateral()
            await self._persist_runtime_state()
            self._last_auto_settlements = []
            events = await self._universe.refresh()
            events = self._apply_event_budget(events)
            self._active_event_ids = {event.event_id for event in events}
            for event in events:
                self._events[event.event_id] = event
            self._exchange.update_universe(events)
            await self._repository.upsert_events_batch(events)

            self._current_cycle_step = "fetching_books"
            self._current_cycle_pct = 0.0

            def _on_progress(pct: float) -> None:
                self._current_cycle_pct = pct

            books = await self._market_data.refresh(events, on_progress=_on_progress)
            # Books are loaded; remaining work (diagnostics, scan, execute, overlay) has no
            # meaningful 0–100% — avoid exposing "100% scanning" while this phase runs.
            self._current_cycle_step = "evaluating"
            self._current_cycle_pct = None
            # Strip synthetic books before scanning so false edges are never scored.
            real_books = {
                token_id: book
                for token_id, book in books.items()
                if book.source != "synthetic"
            }
            self._last_books = dict(books)
            bsrc = _book_source_counts(books)
            if bsrc["books_synthetic"] > 0:
                logger.warning(
                    "arb_engine.synthetic_books_in_cycle",
                    synthetic=bsrc["books_synthetic"],
                    clob=bsrc["books_clob"],
                    other=bsrc["books_other"],
                    total=len(books),
                )
            self._exchange.sync_books(books)
            for book in books.values():
                await self._repository.record_book(book)

            auto_settled = await self._auto_settle_resolved_events_locked()
            complete_set_unwound = await self._maybe_unwind_complete_set_baskets()
            # Recomputed every cycle (after CLOB sync above): deposits/withdrawals on Polymarket move
            # available_cash without restarting the process. Still bounded by MAX_BASKET_NOTIONAL and
            # ARB_BASKET_NOTIONAL_FRACTION_OF_EQUITY vs bankroll.
            base_from_rules = self._effective_base_max_basket_notional()
            base_max = self._clamp_basket_notional_to_available_cash(base_from_rules)
            if self._config.arb_cap_scan_notional_to_available_cash and base_from_rules > base_max + 1.0:
                now_mono = time.monotonic()
                if now_mono - self._last_basket_cash_limit_log_mono >= 300.0:
                    self._last_basket_cash_limit_log_mono = now_mono
                    logger.info(
                        "arb_engine.basket_notional_cash_limited",
                        unclamped_from_rules=round(base_from_rules, 4),
                        after_cash_clamp=round(base_max, 4),
                        available_cash=round(float(self._exchange.available_cash), 4),
                    )
            bankroll_sz = self._equity_bankroll_for_sizing()
            cash_cap = self._spendable_cash_ceiling_for_baskets()
            diagnostics = self._scanner.cycle_diagnostics(
                events, real_books, max_basket_notional=base_max
            )
            mult = max(1.0, float(self._config.arb_max_basket_notional_qualified_multiplier))
            abs_cap = float(self._config.arb_max_basket_notional_qualified_abs_max)
            cycle_basket_cap = base_max
            if mult > 1.0 + 1e-9:
                probe = self._scanner.scan(events, real_books, max_basket_notional=base_max)
                if probe:
                    scaled = base_max * mult
                    if abs_cap > 1e-9:
                        scaled = min(scaled, abs_cap)
                    cycle_basket_cap = max(base_max, scaled)
                    cycle_basket_cap = self._clamp_basket_notional_to_available_cash(cycle_basket_cap)
                    opportunities = self._scanner.scan(
                        events, real_books, max_basket_notional=cycle_basket_cap
                    )
                else:
                    opportunities = probe
            else:
                opportunities = self._scanner.scan(events, real_books, max_basket_notional=base_max)
                cycle_basket_cap = base_max
            self._opportunities = opportunities
            executed = 0

            self._risk.begin_cycle(books_synthetic=bsrc["books_synthetic"])
            if self._risk.cycle_execution_block_reason:
                logger.warning(
                    "arb_engine.execution_skipped_data_quality",
                    reason=self._risk.cycle_execution_block_reason,
                    synthetic=bsrc["books_synthetic"],
                )

            for opportunity in opportunities:
                if executed >= self._config.max_opportunities_per_cycle:
                    await self._repository.record_opportunity(
                        opportunity,
                        decision="skipped_cycle_cap",
                        reason=f"cycle execution cap reached ({self._config.max_opportunities_per_cycle})",
                    )
                    continue
                open_baskets = self._open_basket_count()
                approved, reason = self._risk.approve(
                    opportunity,
                    self._exchange,
                    open_baskets,
                    open_baskets_by_strategy=self._open_basket_count_by_strategy(),
                    max_basket_notional=cycle_basket_cap,
                )
                await self._repository.record_opportunity(
                    opportunity,
                    decision="approved" if approved else "rejected",
                    reason="" if approved else reason,
                )
                if not approved:
                    continue
                basket = await self._execute_opportunity(opportunity)
                if basket is not None:
                    executed += 1
                    self._risk.record_execution_success()
                    self._risk.record_execution(opportunity)

            cfg = self._config
            live = bool(cfg.arb_live_execution) and not bool(cfg.paper_trade)
            # Paper: optional news / copy sleeves. Live: skip overlay (paper-only); trader-follow
            # only if both ENABLE_TRADER_FOLLOW and TRADER_FOLLOW_ALLOW_LIVE are set.
            if not live:
                from ..alpha.overlay import run_directional_overlay

                await run_directional_overlay(
                    self,
                    events,
                    books,
                    real_books,
                    len(opportunities),
                    executed,
                )
            if not live or (cfg.enable_trader_follow and cfg.trader_follow_allow_live):
                from ..alpha.trader_follow import run_trader_follow

                await run_trader_follow(
                    self,
                    events,
                    books,
                    real_books,
                    len(opportunities),
                    executed,
                )

            await self._repository.replace_positions(self._exchange.get_positions())
            await self._persist_runtime_state()
            self._last_cycle_at = utc_now()
            self._last_cycle_summary = {
                "timestamp": self._last_cycle_at.isoformat(),
                "cycle_elapsed_seconds": round(time.monotonic() - cycle_t0, 3),
                "tracked_events": len(events),
                "tracked_books": len(books),
                "auto_settled": auto_settled,
                "complete_set_unwound": complete_set_unwound,
                "opportunities": len(opportunities),
                "executed": executed,
                "effective_max_basket_notional": round(cycle_basket_cap, 4),
                "base_max_basket_notional": round(base_max, 4),
                "basket_notional_before_cash_clamp": round(base_from_rules, 4),
                "equity_bankroll_for_sizing": round(bankroll_sz, 4),
                "available_cash": round(float(self._exchange.available_cash), 4),
                "available_cash_ceiling_for_baskets": (
                    None if cash_cap is None else round(float(cash_cap), 4)
                ),
                "cap_scan_notional_to_available_cash": bool(
                    self._config.arb_cap_scan_notional_to_available_cash
                ),
                "strategy_mode": str(self._config.arb_strategy_mode),
                "adaptive_event_target": int(self._adaptive_event_target),
                "basket_notional_fraction_of_equity": float(
                    self._config.arb_basket_notional_fraction_of_equity
                ),
                "diagnostics": diagnostics,
                **bsrc,
            }
            self._update_adaptive_event_target(self._last_cycle_summary)
            logger.info(
                "arb_engine.cycle_done",
                tracked_events=len(events),
                opportunities=len(opportunities),
                executed=executed,
                complete_set_unwound=complete_set_unwound,
                books_clob=bsrc["books_clob"],
                books_synthetic=bsrc["books_synthetic"],
                books_other=bsrc["books_other"],
            )
            if self._config.arb_log_cycle_diagnostics:
                logger.info("arb_engine.cycle_diagnostics", **diagnostics)
            await self._append_paper_equity_snapshot()
            self._current_cycle_step = "idle"
            self._current_cycle_pct = 0.0
            return dict(self._last_cycle_summary)

    def _apply_event_budget(self, events: list[ArbEvent]) -> list[ArbEvent]:
        if not self._config.arb_adaptive_event_budget_enabled:
            self._adaptive_event_target = int(self._config.max_tracked_events)
            return events
        lo = max(1, int(self._config.arb_adaptive_event_budget_min))
        hi = max(lo, int(self._config.arb_adaptive_event_budget_max))
        self._adaptive_event_target = max(lo, min(hi, int(self._adaptive_event_target)))
        if len(events) <= self._adaptive_event_target:
            return events
        return events[: self._adaptive_event_target]

    def _update_adaptive_event_target(self, cycle_summary: dict[str, Any]) -> None:
        if not self._config.arb_adaptive_event_budget_enabled:
            return
        lo = max(1, int(self._config.arb_adaptive_event_budget_min))
        hi = max(lo, int(self._config.arb_adaptive_event_budget_max))
        target_sec = max(15.0, float(self._config.arb_adaptive_event_target_cycle_seconds))
        elapsed = float(cycle_summary.get("cycle_elapsed_seconds") or 0.0)
        books_total = max(1, int(cycle_summary.get("tracked_books") or 0))
        books_syn = int(cycle_summary.get("books_synthetic") or 0)
        syn_ratio = books_syn / books_total

        new_target = int(self._adaptive_event_target)
        # Under pressure: long cycles or lots of synthetic books -> scale down.
        if elapsed > target_sec * 1.25 or syn_ratio >= 0.35:
            new_target = int(max(lo, round(new_target * 0.86)))
        # Headroom: fast cycle and real books mostly available -> scale up.
        elif elapsed > 0 and elapsed < target_sec * 0.72 and syn_ratio <= 0.18:
            new_target = int(min(hi, round(new_target * 1.12)))
        self._adaptive_event_target = max(lo, min(hi, new_target))


    async def _append_paper_equity_snapshot(self) -> None:
        if not self._config.paper_trade or not self._config.paper_equity_snapshot_log:
            return
        path = (self._config.paper_equity_log_path or "").strip()
        if not path:
            return
        record = {
            "ts": self._last_cycle_at.isoformat() if self._last_cycle_at else None,
            "summary": self.summary(),
            "last_cycle": dict(self._last_cycle_summary),
        }
        line = json.dumps(record, default=str) + "\n"

        def _write() -> None:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(line)

        await asyncio.to_thread(_write)

    async def _execute_opportunity(self, opportunity: ArbOpportunity) -> BasketRecord | None:
        basket = BasketRecord(
            basket_id=f"basket-{uuid.uuid4().hex[:10]}",
            opportunity_id=opportunity.opportunity_id,
            event_id=opportunity.event_id,
            strategy_type=opportunity.strategy_type,
            status="EXECUTING",
            capital_reserved=opportunity.capital_required,
            target_net_edge_bps=opportunity.net_edge_bps,
            notes=opportunity.rationale,
        )
        self._baskets[basket.basket_id] = basket
        await self._repository.create_basket(basket)
        starting_realized = self._exchange.realized_pnl

        try:
            if opportunity.requires_conversion:
                await self._execute_neg_risk_basket(opportunity, basket)
                basket.status = "CLOSED"
                basket.closed_at = utc_now()
            else:
                for leg in opportunity.legs:
                    order = await self._place_leg(
                        basket_id=basket.basket_id,
                        opportunity=opportunity,
                        market_id=leg.market_id,
                        token_id=leg.token_id,
                        contract_side=leg.position_side,
                        side=leg.action,
                        price=leg.price,
                        size=leg.size,
                        fees_enabled=leg.fees_enabled,
                    )
                    if order.status != "filled" or order.filled_size + 1e-12 < leg.size:
                        raise RuntimeError(f"failed to fill complete-set leg {leg.token_id}")
                basket.status = "OPEN"

            total_slippage_bps = 0.0
            leg_count = 0
            for order in self._exchange.all_orders():
                if order.basket_id != basket.basket_id or order.status != "filled" or order.average_price <= 0:
                    continue
                intended = next((leg.price for leg in opportunity.legs if leg.token_id == order.token_id), None)
                if intended and intended > 0:
                    total_slippage_bps += abs(order.average_price - intended) / intended * 10000.0
                    leg_count += 1
            if leg_count > 0:
                basket.fill_slippage_bps = total_slippage_bps / leg_count

            basket.realized_net_pnl = self._exchange.realized_pnl - starting_realized
            await self._repository.update_basket(basket)
            await self._repository.record_opportunity(opportunity, decision="executed", reason="")
            await self._repository.replace_positions(self._exchange.get_positions())
            await self._persist_runtime_state()
            return basket
        except Exception as exc:
            basket.status = "FAILED"
            basket.notes = f"{basket.notes} | failure: {exc}"
            basket.closed_at = utc_now()
            await self._liquidate_event_positions(opportunity.event_id, basket.basket_id, opportunity.opportunity_id)
            basket.realized_net_pnl = self._exchange.realized_pnl - starting_realized
            await self._repository.update_basket(basket)
            await self._repository.record_opportunity(opportunity, decision="failed", reason=str(exc))
            await self._repository.replace_positions(self._exchange.get_positions())
            await self._persist_runtime_state()
            logger.error("arb_engine.execution_failed", event_id=opportunity.event_id, error=str(exc))
            self._risk.record_execution_failure()
            return None

    async def _execute_neg_risk_basket(self, opportunity: ArbOpportunity, basket: BasketRecord) -> None:
        if not self._config.neg_risk_live_onchain_available():
            raise RuntimeError(
                "neg-risk live execution blocked for Gnosis Safe: on-chain conversion is not wired. "
                "Use complete_set mode, EOA (CLOB_SIGNATURE_TYPE=0), or enable ARB_ALLOW_NEG_RISK_LIVE_WITH_SAFE "
                "after integrating Polymarket relayer conversion."
            )
        if not opportunity.convert_from_market_id:
            raise RuntimeError("neg-risk opportunity missing source market")
        logger.info(
            "arb_engine.neg_risk_conversion_start",
            basket_id=basket.basket_id,
            event_id=opportunity.event_id,
            source_market_id=opportunity.convert_from_market_id,
            n_sell_legs=max(0, len(opportunity.legs) - 1),
            hint="Polymarket activity lists buy/convert/sell separately; first line is often one NO buy.",
        )
        buy_leg = opportunity.legs[0]
        buy_order = await self._place_leg(
            basket_id=basket.basket_id,
            opportunity=opportunity,
            market_id=buy_leg.market_id,
            token_id=buy_leg.token_id,
            contract_side=buy_leg.position_side,
            side=buy_leg.action,
            price=buy_leg.price,
            size=buy_leg.size,
            fees_enabled=buy_leg.fees_enabled,
        )
        if buy_order.status != "filled" or buy_order.filled_size + 1e-12 < buy_leg.size:
            raise RuntimeError("source NO leg did not fill")

        event = self._events[opportunity.event_id]
        requested_at = datetime.now(timezone.utc).isoformat()
        outputs = self._exchange.convert_neg_risk(event, opportunity.convert_from_market_id, buy_leg.size)
        completed_at = datetime.now(timezone.utc).isoformat()
        await self._repository.record_conversion(
            basket_id=basket.basket_id,
            event_id=event.event_id,
            input_market_id=opportunity.convert_from_market_id,
            input_token_id=buy_leg.token_id,
            outputs=outputs,
            size=buy_leg.size,
            requested_at=requested_at,
            completed_at=completed_at,
        )

        for leg in opportunity.legs[1:]:
            sell_order = await self._place_leg(
                basket_id=basket.basket_id,
                opportunity=opportunity,
                market_id=leg.market_id,
                token_id=leg.token_id,
                contract_side=leg.position_side,
                side=leg.action,
                price=leg.price,
                size=leg.size,
                fees_enabled=leg.fees_enabled,
            )
            if sell_order.status != "filled" or sell_order.filled_size + 1e-12 < leg.size:
                raise RuntimeError(f"converted YES leg did not fill for {leg.token_id}")

    async def _liquidate_event_positions(self, event_id: str, basket_id: str, opportunity_id: str) -> None:
        for position in list(self._exchange.get_positions()):
            if position.event_id != event_id:
                continue
            book = self._exchange.book_for_token(position.token_id)
            if book is None or book.best_bid <= 0:
                logger.warning(
                    "arb_engine.unwind_position_abandoned",
                    token_id=position.token_id,
                    event_id=event_id,
                    size=round(position.size, 6),
                    avg_price=round(position.avg_price, 6),
                    reason="no_bid_for_unwind",
                    note="Position remains in ledger — manually settle via POST /settle to clear exposure.",
                )
                continue
            intent = OrderIntent(
                basket_id=basket_id,
                opportunity_id=opportunity_id,
                token_id=position.token_id,
                market_id=position.market_id,
                event_id=event_id,
                contract_side=position.contract_side,
                side="SELL",
                price=book.best_bid,
                size=position.size,
                order_type="fak",
                maker_or_taker="taker",
                fees_enabled=book.fees_enabled,
                metadata={"reason": "failure_unwind"},
            )
            order, fills = self._exchange.place_order(intent)
            await self._repository.record_order(order)
            for fill in fills:
                await self._repository.record_fill(fill)

    async def _place_leg(
        self,
        basket_id: str,
        opportunity: ArbOpportunity,
        market_id: str,
        token_id: str,
        contract_side: str,
        side: str,
        price: float,
        size: float,
        fees_enabled: bool,
    ):
        order_type = "fok" if self._config.allow_taker_execution else "gtc"
        maker_or_taker = "taker" if self._config.allow_taker_execution else "maker"
        intent = OrderIntent(
            basket_id=basket_id,
            opportunity_id=opportunity.opportunity_id,
            token_id=token_id,
            market_id=market_id,
            event_id=opportunity.event_id,
            contract_side=contract_side,  # type: ignore[arg-type]
            side=side,  # type: ignore[arg-type]
            price=price,
            size=size,
            order_type=order_type,  # type: ignore[arg-type]
            maker_or_taker=maker_or_taker,  # type: ignore[arg-type]
            fees_enabled=fees_enabled,
            metadata={"strategy_type": opportunity.strategy_type},
        )
        order, fills = self._exchange.place_order(intent)
        await self._repository.record_order(order)
        for fill in fills:
            await self._repository.record_fill(fill)
        return order

    async def settle_event(self, event_id: str, resolution_market_id: str) -> dict[str, Any]:
        async with self._cycle_lock:
            return await self._settle_event_locked(event_id, resolution_market_id)

    async def add_funds(self, amount: float, note: str) -> dict[str, Any]:
        async with self._cycle_lock:
            await self._risk.add_funds(amount, note, self._legacy_db, self._exchange)
            await self._reconcile_nominal_contributed_capital()
            await self._persist_runtime_state()
            eq = round(self._exchange.equity, 4)
            return {
                "ok": True,
                "amount": amount,
                "note": note,
                "new_cash": round(self._exchange.cash, 4),
                "new_equity": eq,
                "new_bankroll": eq,
            }

    def summary(self) -> dict[str, Any]:
        payload = self._risk.summary(self._exchange, self._open_basket_count())
        av = float(payload.get("available_cash", 0.0))
        payload["spendable_cash_usdc"] = round(av, 4)
        if not self._config.paper_trade and hasattr(self._exchange, "last_clob_collateral_usdc"):
            lc = self._exchange.last_clob_collateral_usdc
            if lc is not None:
                lc_f = round(float(lc), 4)
                payload["clob_collateral_usdc"] = lc_f
                # Single source of truth for free USDC on Polymarket after sync.
                payload["available_cash"] = lc_f
                payload["spendable_cash_usdc"] = lc_f
        payload.update(
            {
                "paper_trade": self._config.paper_trade,
                "arb_live_execution": bool(self._config.arb_live_execution),
                "tracked_events": len(self._active_event_ids),
                "last_cycle_at": self._last_cycle_at.isoformat() if self._last_cycle_at else None,
                "last_cycle": dict(self._last_cycle_summary),
                "latest_opportunities": len(self._opportunities),
                "agent_display_name": (self._config.agent_display_name or "").strip(),
                "control_api_port": int(self._config.control_api_port),
                "directional_overlay_enabled": bool(self._config.enable_directional_overlay),
                "directional_overlay_llm_news": bool(self._config.directional_overlay_llm_news),
                "max_tracked_events_config": int(self._config.max_tracked_events),
                "cycle_progress_pct": (
                    None
                    if self._current_cycle_pct is None
                    else round(float(self._current_cycle_pct), 1)
                ),
                "cycle_step": self._current_cycle_step,
            }
        )
        return payload

    def events_snapshot(self) -> list[dict[str, Any]]:
        event_ids = self._active_event_ids or set(self._events)
        return [self._events[event_id].as_dict() for event_id in sorted(event_ids) if event_id in self._events]

    def active_events_snapshot(self) -> list[dict[str, Any]]:
        return [
            self._events[event_id].as_dict()
            for event_id in sorted(self._active_event_ids)
            if event_id in self._events
        ]

    def books_snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            token_id: book.as_dict()
            for token_id, book in self._last_books.items()
        }

    def opportunities_snapshot(self) -> list[dict[str, Any]]:
        return [opportunity.as_dict() for opportunity in self._opportunities]

    def baskets_snapshot(self) -> list[dict[str, Any]]:
        baskets = sorted(self._baskets.values(), key=lambda basket: basket.created_at, reverse=True)
        return [basket.as_dict() for basket in baskets]

    def _open_basket_count(self) -> int:
        return sum(1 for basket in self._baskets.values() if basket.status in {"OPEN", "EXECUTING"})

    def _open_basket_count_by_strategy(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for basket in self._baskets.values():
            if basket.status in {"OPEN", "EXECUTING"}:
                counts[basket.strategy_type] = counts.get(basket.strategy_type, 0) + 1
        return counts

    def cycle_snapshot(self) -> dict[str, Any]:
        return {
            "timestamp": utc_now().isoformat(),
            "summary": self.summary(),
            "events": self.events_snapshot(),
            "active_events": self.active_events_snapshot(),
            "books": self.books_snapshot(),
            "opportunities": self.opportunities_snapshot(),
            "baskets": self.baskets_snapshot(),
            "orders": [order.as_dict() for order in self._exchange.get_orders()],
            "positions": [position.as_dict() for position in self._exchange.get_positions()],
            "auto_settlements": list(self._last_auto_settlements),
        }

    async def _persist_runtime_state(self) -> None:
        snapshot = self._exchange.snapshot_state()
        await self._repository.save_runtime_state(
            cash=snapshot["cash"],
            contributed_capital=snapshot["contributed_capital"],
            realized_pnl=snapshot["realized_pnl"],
            fees_paid=snapshot["fees_paid"],
            rebates_earned=snapshot["rebates_earned"],
            updated_at=utc_now().isoformat(),
        )
        await self._repository.save_cooldowns(self._risk.cooldowns)

    def _log_complete_set_hold(self, *, dedupe_key: str, reason: str, **fields: Any) -> None:
        interval_sec = float(self._config.arb_log_complete_set_hold_interval_seconds)
        if interval_sec <= 0:
            return
        now = time.monotonic()
        k = f"{dedupe_key}:{reason}"
        last = self._complete_set_hold_log_mono.get(k, 0.0)
        if now - last < interval_sec:
            return
        self._complete_set_hold_log_mono[k] = now
        logger.info("arb_engine.complete_set_hold", reason=reason, dedupe_key=dedupe_key, **fields)

    def _complete_set_unwind_block_detail(
        self,
        leg_rows: list[tuple[OutcomeMarket, PositionRecord]],
    ) -> tuple[str, dict[str, Any]] | None:
        """If unwind metrics cannot be computed, return (reason, extra) for logging."""
        unified_size = min(p.size for _, p in leg_rows)
        if unified_size <= 1e-9:
            return "zero_unified_size", {"unified_size": unified_size}
        for market, _pos in leg_rows:
            book = self._exchange.book_for_token(market.yes_token_id)
            if book is None:
                return "no_book_for_leg", {
                    "token_id": market.yes_token_id,
                    "market_id": market.market_id,
                }
            if book.best_bid <= 0:
                return "no_bid", {"token_id": market.yes_token_id, "source": book.source}
            if book.source != "clob":
                return "non_clob_book", {"token_id": market.yes_token_id, "source": book.source}
        cost = sum(pos.avg_price * unified_size for _, pos in leg_rows)
        if cost <= 1e-12:
            return "zero_cost", {}
        return None

    def _complete_set_leg_rows(self, event: ArbEvent) -> list[tuple[OutcomeMarket, PositionRecord]] | None:
        """YES positions for every outcome market, or None if incomplete."""
        by_token = {p.token_id: p for p in self._exchange.get_positions()}
        leg_rows: list[tuple[OutcomeMarket, PositionRecord]] = []
        for market in event.markets:
            pos = by_token.get(market.yes_token_id)
            if pos is None or pos.contract_side != "YES":
                return None
            leg_rows.append((market, pos))
        if len(leg_rows) != len(event.markets):
            return None
        return leg_rows

    def _complete_set_unwind_metrics(
        self,
        leg_rows: list[tuple[OutcomeMarket, PositionRecord]],
    ) -> tuple[float, float, float, float, float, float] | None:
        """
        Returns (unified_size, bid_sum, net_after_fees, cost, profit_est, profit_bps)
        or None if books unusable.
        """
        cfg = self._config
        unified_size = min(p.size for _, p in leg_rows)
        if unified_size <= 1e-9:
            return None

        bid_sum = 0.0
        net_after_fees = 0.0
        cost = 0.0
        for market, pos in leg_rows:
            book = self._exchange.book_for_token(market.yes_token_id)
            if book is None or book.best_bid <= 0 or book.source != "clob":
                return None
            bid_sum += book.best_bid
            leg_gross = unified_size * book.best_bid
            fee = taker_fee_on_notional(leg_gross, book.fees_enabled, cfg.paper_taker_fee_bps)
            net_after_fees += leg_gross - fee
            cost += pos.avg_price * unified_size

        if cost <= 1e-12:
            return None
        profit_est = net_after_fees - cost
        profit_bps = (profit_est / cost) * 10000.0
        return unified_size, bid_sum, net_after_fees, cost, profit_est, profit_bps

    def _should_unwind_complete_set_now(
        self,
        *,
        net_after_fees: float,
        bid_sum: float,
        cost: float,
        unified_size: float,
        profit_est: float,
        profit_bps: float,
    ) -> bool:
        cfg = self._config
        if cfg.complete_set_unwind_vs_resolution:
            resolution_usd = unified_size * 1.0
            eps = max(0.0, float(cfg.complete_set_unwind_vs_resolution_epsilon_usd))
            return net_after_fees + 1e-9 >= resolution_usd - eps

        min_sum = float(cfg.complete_set_unwind_min_bid_sum)
        min_profit_bps = float(cfg.complete_set_unwind_min_profit_bps)
        min_gross_bps = float(cfg.complete_set_unwind_min_gross_recovery_bps)
        min_est_net = float(cfg.complete_set_unwind_min_est_net_usd)
        stop_bps = float(cfg.complete_set_unwind_stop_loss_bps)

        bid_ok = min_sum > 0 and bid_sum >= min_sum
        profit_ok = (
            min_profit_bps > 0
            and profit_bps >= min_profit_bps
            and profit_est >= min_est_net
        )
        gross_recovery_bps = ((bid_sum * unified_size) - cost) / cost * 10000.0
        gross_ok = (
            min_gross_bps > 0
            and gross_recovery_bps >= min_gross_bps
            and profit_est >= min_est_net
        )
        stop_ok = False
        if stop_bps > 0 and cost > 1e-12 and profit_est < 0:
            loss_bps = (-profit_est / cost) * 10000.0
            stop_ok = loss_bps >= stop_bps

        min_any = float(cfg.complete_set_unwind_min_any_profit_usd)
        any_profit_ok = min_any > 0 and profit_est >= min_any

        if (
            min_sum <= 0
            and min_profit_bps <= 0
            and min_gross_bps <= 0
            and stop_bps <= 0
            and min_any <= 0
        ):
            return False
        return bid_ok or profit_ok or gross_ok or stop_ok or any_profit_ok

    async def _execute_complete_set_unwind_sells(
        self,
        *,
        basket_id: str,
        opportunity_id: str,
        event_id: str,
        leg_rows: list[tuple[OutcomeMarket, PositionRecord]],
        unified_size: float,
    ) -> list[OrderRecord]:
        sell_orders: list[OrderRecord] = []
        for market, _pos in leg_rows:
            book = self._exchange.book_for_token(market.yes_token_id)
            if book is None:
                raise RuntimeError(f"missing book for {market.yes_token_id}")
            intent = OrderIntent(
                basket_id=basket_id,
                opportunity_id=opportunity_id,
                token_id=market.yes_token_id,
                market_id=market.market_id,
                event_id=event_id,
                contract_side="YES",
                side="SELL",
                price=book.best_bid,
                size=unified_size,
                order_type="fak",
                maker_or_taker="taker",
                fees_enabled=book.fees_enabled,
                metadata={"reason": "complete_set_auto_unwind"},
            )
            order, fills = self._exchange.place_order(intent)
            await self._repository.record_order(order)
            for fill in fills:
                await self._repository.record_fill(fill)
            sell_orders.append(order)
        return sell_orders

    async def _maybe_unwind_complete_set_baskets(self) -> int:
        """
        FAK-sell full YES complete sets when bid / net / gross / any-profit / stop-loss triggers fire.

        This is rule-based (thresholds), not an optimal "maximize exit PnL vs resolution" solver.
        If no trigger fires, the basket is held — including through resolution when appropriate.

        1) OPEN baskets (strategy complete_set).
        2) Orphan positions: same event has a full YES set in the exchange ledger but no OPEN
           basket (e.g. DB reset, other process, or legacy row) — creates a synthetic basket for audit.

        Requires real CLOB books. Runs unattended each cycle when not halted.
        """
        cfg = self._config
        if not cfg.complete_set_auto_unwind:
            return 0
        if self._risk.halted:
            return 0

        min_sum = float(cfg.complete_set_unwind_min_bid_sum)
        min_profit_bps = float(cfg.complete_set_unwind_min_profit_bps)
        min_gross_bps = float(cfg.complete_set_unwind_min_gross_recovery_bps)
        stop_bps = float(cfg.complete_set_unwind_stop_loss_bps)
        min_any_usd = float(cfg.complete_set_unwind_min_any_profit_usd)
        if not cfg.complete_set_unwind_vs_resolution and (
            min_sum <= 0
            and min_profit_bps <= 0
            and min_gross_bps <= 0
            and stop_bps <= 0
            and min_any_usd <= 0
        ):
            return 0

        unwound = 0
        processed_event_ids: set[str] = set()

        async def _finalize_one(
            basket: BasketRecord,
            *,
            event_id: str,
            leg_rows: list[tuple[OutcomeMarket, PositionRecord]],
            unified_size: float,
            bid_sum: float,
            profit_est: float,
            sell_orders: list[OrderRecord],
            pnl_before: float,
        ) -> None:
            nonlocal unwound
            min_ratio = (
                min(o.filled_size / unified_size for o in sell_orders) if sell_orders else 0.0
            )
            if min_ratio < 0.99:
                logger.warning(
                    "arb_engine.complete_set_unwind_partial",
                    basket_id=basket.basket_id,
                    event_id=event_id,
                    min_fill_ratio=round(min_ratio, 4),
                )
                basket.notes = (
                    f"{basket.notes} | partial auto-unwind min_fill_ratio={min_ratio:.3f}"
                ).strip()
                basket.realized_net_pnl += self._exchange.realized_pnl - pnl_before
                if basket.status == "EXECUTING":
                    basket.status = "OPEN"
                await self._repository.update_basket(basket)
                await self._repository.replace_positions(self._exchange.get_positions())
                await self._persist_runtime_state()
                return

            basket.status = "CLOSED"
            basket.closed_at = utc_now()
            basket.realized_net_pnl += self._exchange.realized_pnl - pnl_before
            basket.notes = (
                f"{basket.notes} | auto-unwind bid_sum={bid_sum:.3f} est_profit=${profit_est:.2f}"
            ).strip()
            await self._repository.update_basket(basket)
            await self._repository.replace_positions(self._exchange.get_positions())
            await self._persist_runtime_state()
            unwound += 1
            logger.info(
                "arb_engine.complete_set_unwind_done",
                basket_id=basket.basket_id,
                event_id=event_id,
            )

        # ── Pass A: OPEN complete-set baskets ─────────────────────────────
        for basket in list(self._baskets.values()):
            if basket.status != "OPEN" or basket.strategy_type != "complete_set":
                continue
            event = self._events.get(basket.event_id)
            if event is None or not event.markets:
                self._log_complete_set_hold(
                    dedupe_key=basket.basket_id,
                    reason="event_not_in_universe",
                    basket_id=basket.basket_id,
                    event_id=basket.event_id,
                    note="Event dropped from tracked universe; unwind needs event metadata + books.",
                )
                continue
            leg_rows = self._complete_set_leg_rows(event)
            if leg_rows is None:
                self._log_complete_set_hold(
                    dedupe_key=basket.basket_id,
                    reason="incomplete_yes_set",
                    basket_id=basket.basket_id,
                    event_id=basket.event_id,
                    note="Need a YES position on every outcome leg with equal tradeable size.",
                )
                continue
            blk = self._complete_set_unwind_block_detail(leg_rows)
            if blk is not None:
                br, extra = blk
                self._log_complete_set_hold(
                    dedupe_key=basket.basket_id,
                    reason=br,
                    basket_id=basket.basket_id,
                    event_id=basket.event_id,
                    **extra,
                )
                continue
            m = self._complete_set_unwind_metrics(leg_rows)
            if m is None:
                self._log_complete_set_hold(
                    dedupe_key=basket.basket_id,
                    reason="metrics_unavailable",
                    basket_id=basket.basket_id,
                    event_id=basket.event_id,
                )
                continue
            unified_size, bid_sum, net_after_fees, cost, profit_est, profit_bps = m
            gross_recovery_bps = ((bid_sum * unified_size) - cost) / cost * 10000.0
            if not self._should_unwind_complete_set_now(
                net_after_fees=net_after_fees,
                bid_sum=bid_sum,
                cost=cost,
                unified_size=unified_size,
                profit_est=profit_est,
                profit_bps=profit_bps,
            ):
                vs_res = cfg.complete_set_unwind_vs_resolution
                hold_extras: dict[str, Any] = {
                    "bid_sum": round(bid_sum, 4),
                    "profit_est_usd": round(profit_est, 4),
                    "profit_bps": round(profit_bps, 2),
                    "gross_recovery_bps": round(gross_recovery_bps, 2),
                    "unified_size": round(unified_size, 4),
                }
                if vs_res:
                    hold_extras["net_sell_after_fees_usd"] = round(net_after_fees, 4)
                    hold_extras["resolution_payout_usd"] = round(unified_size, 4)
                    hold_extras["vs_resolution_epsilon_usd"] = float(
                        cfg.complete_set_unwind_vs_resolution_epsilon_usd
                    )
                    hold_note = (
                        "Holding: CLOB net sell (after fees) below resolution payout minus epsilon "
                        "(COMPLETE_SET_UNWIND_VS_RESOLUTION)."
                    )
                else:
                    hold_extras["thresholds"] = {
                        "min_bid_sum": min_sum,
                        "min_profit_bps": min_profit_bps,
                        "min_gross_recovery_bps": min_gross_bps,
                        "stop_loss_bps": stop_bps,
                        "min_any_profit_usd": min_any_usd,
                    }
                    hold_note = "Holding: no legacy COMPLETE_SET_UNWIND_MIN_* trigger fired."
                self._log_complete_set_hold(
                    dedupe_key=basket.basket_id,
                    reason="triggers_not_met",
                    basket_id=basket.basket_id,
                    event_id=basket.event_id,
                    note=hold_note,
                    **hold_extras,
                )
                continue

            logger.info(
                "arb_engine.complete_set_unwind_start",
                basket_id=basket.basket_id,
                event_id=basket.event_id,
                source="open_basket",
                bid_sum=round(bid_sum, 4),
                profit_est_usd=round(profit_est, 4),
                profit_bps=round(profit_bps, 2),
                unified_size=round(unified_size, 4),
            )
            pnl_before = self._exchange.realized_pnl
            try:
                sell_orders = await self._execute_complete_set_unwind_sells(
                    basket_id=basket.basket_id,
                    opportunity_id=basket.opportunity_id,
                    event_id=basket.event_id,
                    leg_rows=leg_rows,
                    unified_size=unified_size,
                )
            except Exception as exc:
                logger.error(
                    "arb_engine.complete_set_unwind_failed",
                    basket_id=basket.basket_id,
                    event_id=basket.event_id,
                    error=str(exc),
                )
                continue

            processed_event_ids.add(basket.event_id)
            await _finalize_one(
                basket,
                event_id=basket.event_id,
                leg_rows=leg_rows,
                unified_size=unified_size,
                bid_sum=bid_sum,
                profit_est=profit_est,
                sell_orders=sell_orders,
                pnl_before=pnl_before,
            )

        # ── Pass B: orphan complete sets (positions only, same event not processed) ──
        position_event_ids = {
            p.event_id for p in self._exchange.get_positions() if p.contract_side == "YES" and p.size > 1e-9
        }
        for event_id in sorted(position_event_ids):
            if event_id in processed_event_ids:
                continue
            event = self._events.get(event_id)
            if event is None or not event.markets:
                self._log_complete_set_hold(
                    dedupe_key=f"orphan:{event_id}",
                    reason="event_not_in_universe",
                    event_id=event_id,
                    note="Orphan complete set: event not in universe.",
                )
                continue
            leg_rows = self._complete_set_leg_rows(event)
            if leg_rows is None:
                self._log_complete_set_hold(
                    dedupe_key=f"orphan:{event_id}",
                    reason="incomplete_yes_set",
                    event_id=event_id,
                )
                continue
            blk = self._complete_set_unwind_block_detail(leg_rows)
            if blk is not None:
                br, extra = blk
                self._log_complete_set_hold(
                    dedupe_key=f"orphan:{event_id}",
                    reason=br,
                    event_id=event_id,
                    **extra,
                )
                continue
            m = self._complete_set_unwind_metrics(leg_rows)
            if m is None:
                self._log_complete_set_hold(
                    dedupe_key=f"orphan:{event_id}",
                    reason="metrics_unavailable",
                    event_id=event_id,
                )
                continue
            unified_size, bid_sum, net_after_fees, cost, profit_est, profit_bps = m
            gross_recovery_bps = ((bid_sum * unified_size) - cost) / cost * 10000.0
            if not self._should_unwind_complete_set_now(
                net_after_fees=net_after_fees,
                bid_sum=bid_sum,
                cost=cost,
                unified_size=unified_size,
                profit_est=profit_est,
                profit_bps=profit_bps,
            ):
                vs_res = cfg.complete_set_unwind_vs_resolution
                o_extras: dict[str, Any] = {
                    "bid_sum": round(bid_sum, 4),
                    "profit_est_usd": round(profit_est, 4),
                    "profit_bps": round(profit_bps, 2),
                    "gross_recovery_bps": round(gross_recovery_bps, 2),
                    "unified_size": round(unified_size, 4),
                }
                if vs_res:
                    o_extras["net_sell_after_fees_usd"] = round(net_after_fees, 4)
                    o_extras["resolution_payout_usd"] = round(unified_size, 4)
                    o_extras["vs_resolution_epsilon_usd"] = float(
                        cfg.complete_set_unwind_vs_resolution_epsilon_usd
                    )
                    o_note = "Orphan set: holding — CLOB net below resolution minus epsilon."
                else:
                    o_extras["thresholds"] = {
                        "min_bid_sum": min_sum,
                        "min_profit_bps": min_profit_bps,
                        "min_gross_recovery_bps": min_gross_bps,
                        "stop_loss_bps": stop_bps,
                        "min_any_profit_usd": min_any_usd,
                    }
                    o_note = "Orphan set: holding until a legacy threshold fires."
                self._log_complete_set_hold(
                    dedupe_key=f"orphan:{event_id}",
                    reason="triggers_not_met",
                    event_id=event_id,
                    note=o_note,
                    **o_extras,
                )
                continue

            any_open = next(
                (b for b in self._baskets.values() if b.event_id == event_id and b.status == "OPEN"),
                None,
            )
            if any_open is not None:
                continue

            orphan = BasketRecord(
                basket_id=f"orphan-cs-{uuid.uuid4().hex[:10]}",
                opportunity_id=f"pos-sync-{event_id}",
                event_id=event_id,
                strategy_type="complete_set",
                status="EXECUTING",
                capital_reserved=float(cost),
                target_net_edge_bps=0.0,
                notes="Auto: full YES set in ledger without OPEN basket — synthetic row for unwind audit",
            )
            self._baskets[orphan.basket_id] = orphan
            await self._repository.create_basket(orphan)

            logger.info(
                "arb_engine.complete_set_unwind_start",
                basket_id=orphan.basket_id,
                event_id=event_id,
                source="orphan_positions",
                bid_sum=round(bid_sum, 4),
                profit_est_usd=round(profit_est, 4),
                profit_bps=round(profit_bps, 2),
                unified_size=round(unified_size, 4),
            )
            pnl_before = self._exchange.realized_pnl
            try:
                sell_orders = await self._execute_complete_set_unwind_sells(
                    basket_id=orphan.basket_id,
                    opportunity_id=orphan.opportunity_id,
                    event_id=event_id,
                    leg_rows=leg_rows,
                    unified_size=unified_size,
                )
            except Exception as exc:
                logger.error(
                    "arb_engine.complete_set_unwind_failed",
                    basket_id=orphan.basket_id,
                    event_id=event_id,
                    error=str(exc),
                )
                orphan.status = "FAILED"
                orphan.closed_at = utc_now()
                orphan.notes = f"{orphan.notes} | unwind_error: {exc}"[:2000]
                await self._repository.update_basket(orphan)
                continue

            processed_event_ids.add(event_id)
            await _finalize_one(
                orphan,
                event_id=event_id,
                leg_rows=leg_rows,
                unified_size=unified_size,
                bid_sum=bid_sum,
                profit_est=profit_est,
                sell_orders=sell_orders,
                pnl_before=pnl_before,
            )

        return unwound

    async def _auto_settle_resolved_events_locked(self) -> int:
        if not self._config.auto_settle_resolved_events:
            return 0
        lookup_resolution = getattr(self._universe, "lookup_resolution", None)
        if lookup_resolution is None:
            return 0

        settled = 0
        tracked_event_ids = self._tracked_event_ids_for_resolution()
        for event_id in tracked_event_ids:
            # Do not skip while the event is still in the refreshed universe: Gamma often keeps
            # resolved events in the active list briefly, and we only iterate events we hold.
            known_event = self._events.get(event_id)
            try:
                resolved_event, resolution_market_id, source = await lookup_resolution(
                    event_id,
                    fallback_event=known_event,
                )
            except Exception as exc:
                logger.warning("arb_engine.auto_settle_lookup_failed", event_id=event_id, error=str(exc))
                continue

            if resolved_event is not None:
                self._events[event_id] = resolved_event
            if not resolution_market_id:
                continue

            result = await self._settle_event_locked(event_id, resolution_market_id)
            self._last_auto_settlements.append(
                {
                    **result,
                    "resolution_source": source,
                    "resolved_event": resolved_event.as_dict() if resolved_event is not None else None,
                }
            )
            logger.info(
                "arb_engine.auto_settled",
                event_id=event_id,
                resolution_market_id=resolution_market_id,
                resolution_source=source,
                pnl_realized=result["pnl_realized"],
            )
            settled += 1
        return settled

    def _tracked_event_ids_for_resolution(self) -> list[str]:
        tracked = {position.event_id for position in self._exchange.get_positions()}
        tracked.update(
            basket.event_id
            for basket in self._baskets.values()
            if basket.status in {"OPEN", "EXECUTING"}
        )
        return sorted(tracked)

    async def _settle_event_locked(self, event_id: str, resolution_market_id: str) -> dict[str, Any]:
        event = self._events.get(event_id)
        if event is None:
            raise ValueError(f"unknown event_id {event_id}")
        if not any(market.market_id == resolution_market_id for market in event.markets):
            raise ValueError(
                f"resolution market {resolution_market_id} does not belong to event {event_id}"
            )

        pnl = self._exchange.settle_event(event, resolution_market_id)
        timestamp = datetime.now(timezone.utc).isoformat()
        await self._repository.record_settlement(event_id, resolution_market_id, pnl, timestamp)

        open_event_baskets = [
            basket
            for basket in self._baskets.values()
            if basket.event_id == event_id and basket.status in {"OPEN", "EXECUTING"}
        ]
        total_reserved = sum(basket.capital_reserved for basket in open_event_baskets)
        for basket in open_event_baskets:
            share = pnl * (basket.capital_reserved / total_reserved) if total_reserved > 0 else 0.0
            basket.realized_net_pnl += share
            basket.status = "SETTLED"
            basket.closed_at = utc_now()
            await self._repository.update_basket(basket)

        await self._repository.replace_positions(self._exchange.get_positions())
        await self._persist_runtime_state()
        return {
            "event_id": event_id,
            "resolution_market_id": resolution_market_id,
            "pnl_realized": round(pnl, 4),
            "timestamp": timestamp,
        }
