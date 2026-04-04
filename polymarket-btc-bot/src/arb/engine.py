from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import structlog

from ..config import Settings
from ..storage.db import Database
from .exchange import PaperExchange
from .market_data import ClobMarketDataService
from .models import ArbEvent, ArbOpportunity, BasketRecord, OrderIntent, TokenBook, utc_now
from .pricing import OpportunityScanner
from .repository import ArbRepository
from .risk import ArbRiskManager
from .universe import GammaUniverseService

logger = structlog.get_logger(__name__)


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
        self._exchange = exchange or PaperExchange(config)
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

    @property
    def risk(self) -> ArbRiskManager:
        return self._risk

    @property
    def exchange(self) -> PaperExchange:
        return self._exchange

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self._legacy_db.init()
        await self._repository.init()
        await self._risk.hydrate_from_db(self._legacy_db, self._exchange)
        runtime_state = await self._repository.load_runtime_state()
        if runtime_state:
            positions = await self._repository.load_positions()
            self._exchange.restore_state(runtime_state, positions)
            self._baskets = {
                basket.basket_id: basket
                for basket in await self._repository.load_active_baskets()
            }
        self._risk.cooldowns = await self._repository.load_cooldowns()
        await self._repository.replace_positions(self._exchange.get_positions())
        await self._persist_runtime_state()
        self._risk.capture_session_baseline(self._exchange)
        self._initialized = True

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
            self._last_auto_settlements = []
            events = await self._universe.refresh()
            self._active_event_ids = {event.event_id for event in events}
            for event in events:
                self._events[event.event_id] = event
            self._exchange.update_universe(events)
            for event in events:
                await self._repository.upsert_event(event)

            books = await self._market_data.refresh(events)
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
            diagnostics = self._scanner.cycle_diagnostics(events, real_books)
            opportunities = self._scanner.scan(events, real_books)
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
                open_baskets = self._open_basket_count()
                approved, reason = self._risk.approve(
                    opportunity,
                    self._exchange,
                    open_baskets,
                    open_baskets_by_strategy=self._open_basket_count_by_strategy(),
                )
                await self._repository.record_opportunity(
                    opportunity,
                    decision="approved" if approved else "rejected",
                    reason="" if approved else reason,
                )
                if not approved:
                    continue
                if executed >= self._config.max_opportunities_per_cycle:
                    break
                basket = await self._execute_opportunity(opportunity)
                if basket is not None:
                    executed += 1
                    self._risk.record_execution_success()
                    self._risk.record_execution(opportunity)

            from ..alpha.overlay import run_directional_overlay

            await run_directional_overlay(
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
                "tracked_events": len(events),
                "tracked_books": len(books),
                "auto_settled": auto_settled,
                "opportunities": len(opportunities),
                "executed": executed,
                "diagnostics": diagnostics,
                **bsrc,
            }
            logger.info(
                "arb_engine.cycle_done",
                tracked_events=len(events),
                opportunities=len(opportunities),
                executed=executed,
                books_clob=bsrc["books_clob"],
                books_synthetic=bsrc["books_synthetic"],
                books_other=bsrc["books_other"],
            )
            if self._config.arb_log_cycle_diagnostics:
                logger.info("arb_engine.cycle_diagnostics", **diagnostics)
            await self._append_paper_equity_snapshot()
            return dict(self._last_cycle_summary)

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
            for order in self._exchange._orders.values():
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
        if not opportunity.convert_from_market_id:
            raise RuntimeError("neg-risk opportunity missing source market")
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
        payload.update(
            {
                "paper_trade": self._config.paper_trade,
                "tracked_events": len(self._active_event_ids),
                "last_cycle_at": self._last_cycle_at.isoformat() if self._last_cycle_at else None,
                "last_cycle": dict(self._last_cycle_summary),
                "latest_opportunities": len(self._opportunities),
                "agent_display_name": (self._config.agent_display_name or "").strip(),
                "control_api_port": int(self._config.control_api_port),
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

    async def _auto_settle_resolved_events_locked(self) -> int:
        if not self._config.auto_settle_resolved_events:
            return 0
        lookup_resolution = getattr(self._universe, "lookup_resolution", None)
        if lookup_resolution is None:
            return 0

        settled = 0
        tracked_event_ids = self._tracked_event_ids_for_resolution()
        for event_id in tracked_event_ids:
            if event_id in self._active_event_ids:
                continue
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
