from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from ..config import Settings
from .book_matching import walk_taker_levels
from .fees import maker_rebate_on_notional, taker_fee_on_notional
from .models import ArbEvent, FillRecord, OrderIntent, OrderRecord, PositionRecord, TokenBook, utc_now


class PaperExchange:
    def __init__(self, config: Settings) -> None:
        self._config = config
        self._books: dict[str, TokenBook] = {}
        self._book_timestamps: dict[str, datetime] = {}
        self._orders: dict[str, OrderRecord] = {}
        self._open_orders: set[str] = set()
        self._positions: dict[str, PositionRecord] = {}
        self._token_meta: dict[str, dict[str, str]] = {}
        self._reserved_cash: dict[str, float] = {}
        self._reserved_sell_size: dict[str, float] = {}
        self.cash = float(config.initial_bankroll)
        self.contributed_capital = float(config.initial_bankroll)
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.rebates_earned = 0.0

    def set_starting_cash(self, amount: float) -> None:
        self.cash = float(amount)
        self.contributed_capital = float(amount)
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.rebates_earned = 0.0
        self._orders.clear()
        self._open_orders.clear()
        self._positions.clear()
        self._reserved_cash.clear()
        self._reserved_sell_size.clear()
        self._book_timestamps.clear()

    def add_funds(self, amount: float) -> None:
        self.cash += float(amount)
        self.contributed_capital += float(amount)

    def restore_state(self, state: dict[str, Any], positions: list[PositionRecord]) -> None:
        self._orders.clear()
        self._open_orders.clear()
        self._reserved_cash.clear()
        self._reserved_sell_size.clear()
        self._positions = {
            position.token_id: replace(position)
            for position in positions
            if position.size > 0
        }
        self.cash = float(state.get("cash", self.cash))
        self.contributed_capital = float(state.get("contributed_capital", self.contributed_capital))
        self.realized_pnl = float(state.get("realized_pnl", self.realized_pnl))
        self.fees_paid = float(state.get("fees_paid", self.fees_paid))
        self.rebates_earned = float(state.get("rebates_earned", self.rebates_earned))

    def snapshot_state(self) -> dict[str, float]:
        return {
            "cash": float(self.cash),
            "contributed_capital": float(self.contributed_capital),
            "realized_pnl": float(self.realized_pnl),
            "fees_paid": float(self.fees_paid),
            "rebates_earned": float(self.rebates_earned),
        }

    def update_universe(self, events: list[ArbEvent]) -> None:
        meta: dict[str, dict[str, str]] = dict(self._token_meta)
        for event in events:
            for market in event.markets:
                meta[market.yes_token_id] = {
                    "market_id": market.market_id,
                    "event_id": event.event_id,
                    "outcome_name": market.outcome_name,
                    "contract_side": "YES",
                }
                meta[market.no_token_id] = {
                    "market_id": market.market_id,
                    "event_id": event.event_id,
                    "outcome_name": market.outcome_name,
                    "contract_side": "NO",
                }
        self._token_meta = meta

    def sync_books(self, books: dict[str, TokenBook]) -> None:
        self._books = {token_id: deepcopy(book) for token_id, book in books.items()}
        now = datetime.now(timezone.utc)
        for token_id in books:
            self._book_timestamps[token_id] = now
        self._process_resting_orders()

    @property
    def available_cash(self) -> float:
        return max(self.cash - sum(self._reserved_cash.values()), 0.0)

    @property
    def equity(self) -> float:
        return self.cash + self._mark_to_market_value()

    def _mark_to_market_value(self) -> float:
        value = 0.0
        for position in self._positions.values():
            book = self._books.get(position.token_id)
            mark = book.best_bid if book and book.best_bid > 0 else position.avg_price
            value += position.size * mark
        return value

    def get_positions(self) -> list[PositionRecord]:
        return [replace(position) for position in self._positions.values() if position.size > 0]

    def get_orders(self) -> list[OrderRecord]:
        return [replace(order) for order in self._orders.values()]

    def all_orders(self) -> list[OrderRecord]:
        """Snapshot of all orders (used for basket slippage stats; avoids touching private _orders)."""
        return [replace(order) for order in self._orders.values()]

    def get_open_orders(self) -> list[OrderRecord]:
        return [replace(self._orders[order_id]) for order_id in self._open_orders]

    def book_for_token(self, token_id: str) -> TokenBook | None:
        book = self._books.get(token_id)
        return deepcopy(book) if book is not None else None

    def event_exposure(self, event_id: str) -> float:
        position_cost = sum(
            position.size * position.avg_price
            for position in self._positions.values()
            if position.event_id == event_id
        )
        reserved_cost = 0.0
        for order_id, amount in self._reserved_cash.items():
            order = self._orders.get(order_id)
            if not order:
                continue
            meta = self._token_meta.get(order.token_id, {})
            if meta.get("event_id") == event_id:
                reserved_cost += amount
        return position_cost + reserved_cost

    def place_order(self, intent: OrderIntent) -> tuple[OrderRecord, list[FillRecord]]:
        now = utc_now()
        order_id = f"paper-{uuid.uuid4().hex[:10]}"
        order = OrderRecord(
            order_id=order_id,
            basket_id=intent.basket_id,
            opportunity_id=intent.opportunity_id,
            token_id=intent.token_id,
            market_id=intent.market_id,
            side=intent.side,
            price=float(intent.price),
            size=float(intent.size),
            order_type=intent.order_type,
            maker_or_taker=intent.maker_or_taker,
            status="accepted",
            created_at=now,
            updated_at=now,
            fees_enabled=intent.fees_enabled,
            contract_side=intent.contract_side,
            metadata=dict(intent.metadata),
        )

        if intent.token_id not in self._token_meta:
            order.status = "rejected"
            order.reason = "unknown token"
            self._orders[order.order_id] = order
            return replace(order), []

        book = self._books.get(intent.token_id)
        if book is None:
            order.status = "rejected"
            order.reason = "missing book"
            self._orders[order.order_id] = order
            return replace(order), []

        if intent.side == "BUY":
            estimated_cost = self._reserve_amount_for_order(
                price=intent.price,
                size=intent.size,
                side=intent.side,
                fees_enabled=intent.fees_enabled,
                maker_or_taker=intent.maker_or_taker,
            )
            if self.available_cash + 1e-12 < estimated_cost:
                order.status = "rejected"
                order.reason = "insufficient cash"
                self._orders[order.order_id] = order
                return replace(order), []
        else:
            if self._available_position(intent.token_id) + 1e-12 < intent.size:
                order.status = "rejected"
                order.reason = "insufficient inventory"
                self._orders[order.order_id] = order
                return replace(order), []

        executions, average_price = self._preview_match(order, book)
        filled_size = sum(size for _, size in executions)
        remaining = max(order.size - filled_size, 0.0)

        if order.order_type == "fok" and remaining > 1e-9:
            order.status = "rejected"
            order.reason = "fok_not_filled"
            executions = []
            average_price = 0.0
            filled_size = 0.0

        fills: list[FillRecord] = []
        if executions:
            fills = self._commit_executions(order, executions)
        filled_size = sum(fill.size for fill in fills)
        remaining = max(order.size - filled_size, 0.0)

        order.filled_size = filled_size if fills else 0.0
        order.average_price = average_price if fills else 0.0
        order.updated_at = datetime.now(timezone.utc)

        if order.order_type == "gtc" and remaining > 1e-9:
            self._reserve_remaining(order, remaining)
            order.status = "open" if filled_size == 0 else "partial"
            order.metadata["remaining_size"] = remaining
            self._open_orders.add(order.order_id)
        elif fills and remaining > 1e-9:
            order.status = "partial"
        elif fills:
            order.status = "filled"
        elif order.status != "rejected":
            order.status = "cancelled"
            order.reason = "not_marketable"

        self._orders[order.order_id] = order
        return replace(order), [replace(fill) for fill in fills]

    def cancel_order(self, order_id: str) -> OrderRecord | None:
        order = self._orders.get(order_id)
        if not order:
            return None
        self._release_reservation(order_id)
        self._open_orders.discard(order_id)
        order.status = "cancelled"
        order.updated_at = datetime.now(timezone.utc)
        self._orders[order_id] = order
        return replace(order)

    def cancel_all(self) -> list[OrderRecord]:
        cancelled: list[OrderRecord] = []
        for order_id in list(self._open_orders):
            order = self.cancel_order(order_id)
            if order:
                cancelled.append(order)
        return cancelled

    def convert_neg_risk(self, event: ArbEvent, source_market_id: str, size: float) -> list[dict[str, Any]]:
        source_market = event.market_by_id(source_market_id)
        if source_market is None:
            raise ValueError(f"unknown source market {source_market_id}")

        source_token = source_market.no_token_id
        source_position = self._positions.get(source_token)
        if source_position is None or source_position.size + 1e-12 < size:
            raise ValueError("insufficient NO inventory for conversion")

        total_cost = source_position.avg_price * size
        source_position.size -= size
        if source_position.size <= 1e-12:
            self._positions.pop(source_token, None)
        else:
            source_position.updated_at = utc_now()
            self._positions[source_token] = source_position

        outputs: list[dict[str, Any]] = []
        other_markets = [market for market in event.markets if market.market_id != source_market_id]
        if not other_markets:
            raise ValueError("conversion requires at least one alternate market")
        allocated_cost = total_cost / len(other_markets)

        for market in other_markets:
            existing = self._positions.get(market.yes_token_id)
            output_position = PositionRecord(
                token_id=market.yes_token_id,
                market_id=market.market_id,
                event_id=event.event_id,
                outcome_name=market.outcome_name,
                contract_side="YES",
                size=size,
                avg_price=allocated_cost / size if size else 0.0,
            )
            self._merge_position(output_position)
            outputs.append(
                {
                    "token_id": market.yes_token_id,
                    "market_id": market.market_id,
                    "size": size,
                    "avg_price": output_position.avg_price if existing is None else self._positions[market.yes_token_id].avg_price,
                }
            )
        return outputs

    def settle_event(self, event: ArbEvent, resolution_market_id: str) -> float:
        valid_market_ids = {market.market_id for market in event.markets}
        if resolution_market_id not in valid_market_ids:
            raise ValueError(f"resolution market {resolution_market_id} does not belong to event {event.event_id}")

        for order_id in list(self._open_orders):
            order = self._orders.get(order_id)
            meta = self._token_meta.get(order.token_id, {}) if order is not None else {}
            if order is not None and meta.get("event_id") == event.event_id:
                self.cancel_order(order_id)

        pnl = 0.0
        for market in event.markets:
            for token_id, contract_side in (
                (market.yes_token_id, "YES"),
                (market.no_token_id, "NO"),
            ):
                position = self._positions.get(token_id)
                if position is None:
                    continue
                if contract_side == "YES":
                    payout = position.size if market.market_id == resolution_market_id else 0.0
                else:
                    payout = position.size if market.market_id != resolution_market_id else 0.0
                cost_basis = position.avg_price * position.size
                self.cash += payout
                pnl += payout - cost_basis
                self.realized_pnl += payout - cost_basis
                self._positions.pop(token_id, None)
        return pnl

    def _preview_match(self, order: OrderRecord, book: TokenBook) -> tuple[list[tuple[float, float]], float]:
        executions = self._simulate_executions(order, book)
        total_size = sum(size for _, size in executions)
        if total_size <= 0:
            return [], 0.0
        average_price = sum(price * size for price, size in executions) / total_size
        return executions, average_price

    def _commit_executions(self, order: OrderRecord, executions: list[tuple[float, float]]) -> list[FillRecord]:
        self._consume_book(order.token_id, order.side, executions)
        fills = [
            FillRecord(
                fill_id=f"fill-{uuid.uuid4().hex[:10]}",
                order_id=order.order_id,
                token_id=order.token_id,
                market_id=order.market_id,
                event_id=self._token_meta[order.token_id]["event_id"],
                side=order.side,
                price=price,
                size=size,
                fee_paid=0.0,
                rebate_earned=0.0,
            )
            for price, size in executions
        ]
        for fill in fills:
            self._apply_fill(order, fill)
        return fills

    def _simulate_executions(self, order: OrderRecord, book: TokenBook) -> list[tuple[float, float]]:
        remaining = max(order.size - order.filled_size, 0.0)
        side: str = "BUY" if order.side == "BUY" else "SELL"
        return walk_taker_levels(book, side, float(order.price), remaining)  # type: ignore[arg-type]

    def _consume_book(self, token_id: str, side: str, executions: list[tuple[float, float]]) -> None:
        book = self._books[token_id]
        levels = book.asks if side == "BUY" else book.bids
        for price, quantity in executions:
            remaining = quantity
            for level in levels:
                if remaining <= 1e-12:
                    break
                if abs(level.price - price) > 1e-12:
                    continue
                take = min(level.size, remaining)
                level.size -= take
                remaining -= take
            levels[:] = [level for level in levels if level.size > 1e-12]
        if side == "SELL":
            book.best_bid = levels[0].price if levels else 0.0
        else:
            book.best_ask = levels[0].price if levels else 0.0

    def _apply_fill(self, order: OrderRecord, fill: FillRecord) -> None:
        notional = fill.price * fill.size
        # Log slippage when fill deviates from the order's limit price.
        if order.price > 0:
            slippage_bps = abs(fill.price - order.price) / order.price * 10000.0
            if slippage_bps > 2.0:
                import structlog as _sl

                _sl.get_logger(__name__).warning(
                    "paper_exchange.slippage",
                    order_id=order.order_id,
                    token_id=order.token_id,
                    side=order.side,
                    expected_price=round(order.price, 6),
                    fill_price=round(fill.price, 6),
                    slippage_bps=round(slippage_bps, 2),
                )

        book_ts = self._book_timestamps.get(order.token_id)
        if book_ts is not None:
            age_seconds = (datetime.now(timezone.utc) - book_ts).total_seconds()
            if age_seconds > 30:
                import structlog as _sl

                _sl.get_logger(__name__).warning(
                    "paper_exchange.stale_book_fill",
                    token_id=order.token_id,
                    book_age_seconds=round(age_seconds, 1),
                )

        if order.fees_enabled and order.maker_or_taker == "taker":
            fee_paid = taker_fee_on_notional(notional, True, self._config.paper_taker_fee_bps)
        else:
            fee_paid = 0.0
        spread_cost = 0.0
        if self._config.paper_spread_penalty_bps > 0 and order.side == "BUY":
            spread_cost = (fill.price * fill.size) * (self._config.paper_spread_penalty_bps / 10000.0)
            self.fees_paid += spread_cost
            self.realized_pnl -= spread_cost
        if order.fees_enabled and order.maker_or_taker == "maker":
            rebate = maker_rebate_on_notional(notional, True, self._config.paper_maker_rebate_bps)
        else:
            rebate = 0.0
        fill.fee_paid = fee_paid
        fill.rebate_earned = rebate
        self.fees_paid += fee_paid
        self.rebates_earned += rebate
        self.realized_pnl += rebate - fee_paid

        if order.side == "BUY":
            self.cash -= notional + fee_paid + spread_cost
            self.cash += rebate
            meta = self._token_meta[order.token_id]
            self._merge_position(
                PositionRecord(
                    token_id=order.token_id,
                    market_id=meta["market_id"],
                    event_id=meta["event_id"],
                    outcome_name=meta["outcome_name"],
                    contract_side=meta["contract_side"],  # type: ignore[arg-type]
                    size=fill.size,
                    avg_price=fill.price,
                )
            )
        else:
            position = self._positions.get(order.token_id)
            if position is None or position.size + 1e-12 < fill.size:
                raise ValueError("sell fill without inventory")
            cost_basis = position.avg_price * fill.size
            position.size -= fill.size
            position.updated_at = utc_now()
            if position.size <= 1e-12:
                self._positions.pop(order.token_id, None)
            else:
                self._positions[order.token_id] = position
            self.cash += notional - fee_paid + rebate
            self.realized_pnl += (notional - cost_basis)

    def _merge_position(self, new_position: PositionRecord) -> None:
        existing = self._positions.get(new_position.token_id)
        if existing is None:
            self._positions[new_position.token_id] = new_position
            return
        total_size = existing.size + new_position.size
        if total_size <= 0:
            self._positions.pop(new_position.token_id, None)
            return
        existing.avg_price = (
            (existing.size * existing.avg_price) + (new_position.size * new_position.avg_price)
        ) / total_size
        existing.size = total_size
        existing.updated_at = utc_now()
        self._positions[new_position.token_id] = existing

    def _available_position(self, token_id: str) -> float:
        current = self._positions.get(token_id)
        reserved = sum(
            size for order_id, size in self._reserved_sell_size.items()
            if self._orders.get(order_id) and self._orders[order_id].token_id == token_id
        )
        return max((current.size if current else 0.0) - reserved, 0.0)

    def _reserve_remaining(self, order: OrderRecord, remaining: float) -> None:
        if order.side == "BUY":
            self._reserved_cash[order.order_id] = self._reserve_amount_for_order(
                price=order.price,
                size=remaining,
                side=order.side,
                fees_enabled=order.fees_enabled,
                maker_or_taker=order.maker_or_taker,
            )
        else:
            self._reserved_sell_size[order.order_id] = remaining

    def _release_reservation(self, order_id: str) -> None:
        self._reserved_cash.pop(order_id, None)
        self._reserved_sell_size.pop(order_id, None)

    def _process_resting_orders(self) -> None:
        for order_id in list(self._open_orders):
            order = self._orders.get(order_id)
            if order is None:
                self._open_orders.discard(order_id)
                continue
            remaining = float(order.metadata.get("remaining_size", max(order.size - order.filled_size, 0.0)))
            if remaining <= 1e-12:
                self._release_reservation(order_id)
                self._open_orders.discard(order_id)
                continue

            working_order = replace(order, size=remaining, filled_size=0.0)
            book = self._books.get(order.token_id)
            if book is None:
                continue
            executions, average_price = self._preview_match(working_order, book)
            if not executions:
                continue

            fills = self._commit_executions(order, executions)
            filled = sum(fill.size for fill in fills)
            order.filled_size += filled
            order.average_price = average_price if order.average_price == 0 else ((order.average_price * (order.filled_size - filled)) + (average_price * filled)) / max(order.filled_size, 1e-12)
            remaining = max(order.size - order.filled_size, 0.0)
            order.metadata["remaining_size"] = remaining
            order.updated_at = datetime.now(timezone.utc)
            if remaining <= 1e-12:
                order.status = "filled"
                self._open_orders.discard(order_id)
                self._release_reservation(order_id)
            else:
                order.status = "partial"
                self._reserve_remaining(order, remaining)
            self._orders[order_id] = order

    def _reserve_amount_for_order(
        self,
        *,
        price: float,
        size: float,
        side: str,
        fees_enabled: bool,
        maker_or_taker: str,
    ) -> float:
        notional = float(price) * float(size)
        if side != "BUY":
            return 0.0
        fee = taker_fee_on_notional(notional, fees_enabled and maker_or_taker == "taker", self._config.paper_taker_fee_bps)
        spread_cost = 0.0
        if self._config.paper_spread_penalty_bps > 0:
            spread_cost = notional * (self._config.paper_spread_penalty_bps / 10000.0)
        return notional + fee + spread_cost
