from __future__ import annotations

from ..config import Settings
from .models import ArbEvent, ArbOpportunity, OpportunityLeg, TokenBook


def _buy_fee(price: float, size: float, fees_enabled: bool, config: Settings) -> float:
    if not fees_enabled:
        return 0.0
    return price * size * (config.paper_taker_fee_bps / 10000.0)


def _sell_fee(price: float, size: float, fees_enabled: bool, config: Settings) -> float:
    if not fees_enabled:
        return 0.0
    return price * size * (config.paper_taker_fee_bps / 10000.0)


def _edge_bps(profit: float, capital: float) -> float:
    if capital <= 0:
        return 0.0
    return (profit / capital) * 10000.0


def _normalize_outcome(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def _is_excluded_neg_risk_outcome(value: str) -> bool:
    normalized = _normalize_outcome(value)
    return normalized in {"", "other", "others"}


class OpportunityScanner:
    def __init__(self, config: Settings) -> None:
        self._config = config

    def scan(self, events: list[ArbEvent], books: dict[str, TokenBook]) -> list[ArbOpportunity]:
        opportunities: list[ArbOpportunity] = []
        for event in events:
            opportunities.extend(self._complete_set_opportunities(event, books))
            opportunities.extend(self._neg_risk_opportunities(event, books))

        opportunities.sort(key=lambda opp: (opp.net_edge_bps, opp.expected_profit), reverse=True)
        return opportunities[: self._config.max_opportunities_per_cycle * 10]

    def _complete_set_opportunities(self, event: ArbEvent, books: dict[str, TokenBook]) -> list[ArbOpportunity]:
        normalized_outcomes = [_normalize_outcome(market.outcome_name) for market in event.markets]
        if any(not outcome for outcome in normalized_outcomes):
            return []
        if len(set(normalized_outcomes)) != len(normalized_outcomes):
            return []

        yes_asks: list[tuple[float, OpportunityLeg, TokenBook]] = []
        total_fees_per_unit = 0.0
        for market in event.markets:
            book = books.get(market.yes_token_id)
            if book is None or book.best_ask <= 0:
                return []
            fee = _buy_fee(book.best_ask, 1.0, market.fees_enabled, self._config)
            total_fees_per_unit += fee
            yes_asks.append(
                (
                    book.best_ask,
                    OpportunityLeg(
                        market_id=market.market_id,
                        token_id=market.yes_token_id,
                        outcome_name=market.outcome_name,
                        position_side="YES",
                        action="BUY",
                        price=book.best_ask,
                        size=0.0,
                        fees_enabled=market.fees_enabled,
                    ),
                    book,
                )
            )

        unit_cost = sum(price for price, _, _ in yes_asks)
        expected_profit_per_unit = 1.0 - unit_cost - total_fees_per_unit
        net_edge_bps = _edge_bps(expected_profit_per_unit, unit_cost + total_fees_per_unit)
        if net_edge_bps < self._config.min_complete_set_edge_bps or expected_profit_per_unit <= 0:
            return []

        depth_limit = min(book.available_to_buy(leg.price) for _, leg, book in yes_asks)
        capital_limit = self._config.max_basket_notional / max(unit_cost + total_fees_per_unit, 0.01)
        size = max(min(depth_limit, capital_limit), 0.0)
        if size <= 0:
            return []

        legs = [
            OpportunityLeg(
                market_id=leg.market_id,
                token_id=leg.token_id,
                outcome_name=leg.outcome_name,
                position_side=leg.position_side,
                action=leg.action,
                price=leg.price,
                size=size,
                fees_enabled=leg.fees_enabled,
            )
            for _, leg, _ in yes_asks
        ]
        capital_required = (unit_cost + total_fees_per_unit) * size
        expected_profit = expected_profit_per_unit * size
        return [
            ArbOpportunity(
                strategy_type="complete_set",
                event_id=event.event_id,
                event_title=event.title,
                gross_edge_bps=_edge_bps(1.0 - unit_cost, unit_cost),
                net_edge_bps=net_edge_bps,
                capital_required=capital_required,
                expected_profit=expected_profit,
                legs=legs,
                rationale=f"Buy every YES leg for {unit_cost:.4f} and settle the complete set for 1.0000",
                requires_conversion=False,
                settle_on_resolution=True,
                cooldown_key=f"complete_set:{event.event_id}",
            )
        ]

    def _neg_risk_opportunities(self, event: ArbEvent, books: dict[str, TokenBook]) -> list[ArbOpportunity]:
        if not (event.neg_risk or event.enable_neg_risk):
            return []
        if event.neg_risk_augmented:
            return []
        if any(_is_excluded_neg_risk_outcome(market.outcome_name) for market in event.markets):
            return []

        opportunities: list[ArbOpportunity] = []
        for source_market in event.markets:
            no_book = books.get(source_market.no_token_id)
            if no_book is None or no_book.best_ask <= 0:
                continue

            sell_legs: list[tuple[float, OpportunityLeg, TokenBook]] = []
            total_sell_fee_per_unit = 0.0
            for market in event.markets:
                if market.market_id == source_market.market_id:
                    continue
                yes_book = books.get(market.yes_token_id)
                if yes_book is None or yes_book.best_bid <= 0:
                    sell_legs = []
                    break
                fee = _sell_fee(yes_book.best_bid, 1.0, market.fees_enabled, self._config)
                total_sell_fee_per_unit += fee
                sell_legs.append(
                    (
                        yes_book.best_bid,
                        OpportunityLeg(
                            market_id=market.market_id,
                            token_id=market.yes_token_id,
                            outcome_name=market.outcome_name,
                            position_side="YES",
                            action="SELL",
                            price=yes_book.best_bid,
                            size=0.0,
                            fees_enabled=market.fees_enabled,
                        ),
                        yes_book,
                    )
                )
            if not sell_legs:
                continue

            buy_fee = _buy_fee(no_book.best_ask, 1.0, source_market.fees_enabled, self._config)
            expected_sale_value = sum(price for price, _, _ in sell_legs)
            expected_profit_per_unit = expected_sale_value - no_book.best_ask - buy_fee - total_sell_fee_per_unit
            capital_per_unit = no_book.best_ask + buy_fee
            net_edge_bps = _edge_bps(expected_profit_per_unit, capital_per_unit)
            if net_edge_bps < self._config.min_neg_risk_edge_bps or expected_profit_per_unit <= 0:
                continue

            depth_limits = [no_book.available_to_buy(no_book.best_ask)]
            depth_limits.extend(book.available_to_sell(leg.price) for _, leg, book in sell_legs)
            capital_limit = self._config.max_basket_notional / max(capital_per_unit, 0.01)
            size = max(min(*depth_limits, capital_limit), 0.0)
            if size <= 0:
                continue

            buy_leg = OpportunityLeg(
                market_id=source_market.market_id,
                token_id=source_market.no_token_id,
                outcome_name=source_market.outcome_name,
                position_side="NO",
                action="BUY",
                price=no_book.best_ask,
                size=size,
                fees_enabled=source_market.fees_enabled,
            )
            sell_legs_sized = [
                OpportunityLeg(
                    market_id=leg.market_id,
                    token_id=leg.token_id,
                    outcome_name=leg.outcome_name,
                    position_side=leg.position_side,
                    action=leg.action,
                    price=leg.price,
                    size=size,
                    fees_enabled=leg.fees_enabled,
                )
                for _, leg, _ in sell_legs
            ]
            opportunities.append(
                ArbOpportunity(
                    strategy_type="neg_risk_conversion",
                    event_id=event.event_id,
                    event_title=event.title,
                    gross_edge_bps=_edge_bps(expected_sale_value - no_book.best_ask, no_book.best_ask),
                    net_edge_bps=net_edge_bps,
                    capital_required=capital_per_unit * size,
                    expected_profit=expected_profit_per_unit * size,
                    legs=[buy_leg, *sell_legs_sized],
                    rationale=(
                        f"Buy NO on {source_market.outcome_name} at {no_book.best_ask:.4f}, "
                        f"convert, then sell the other YES legs for {expected_sale_value:.4f}"
                    ),
                    requires_conversion=True,
                    settle_on_resolution=False,
                    convert_from_market_id=source_market.market_id,
                    convert_to_market_ids=[leg.market_id for leg in sell_legs_sized],
                    cooldown_key=f"neg_risk:{event.event_id}:{source_market.market_id}",
                )
            )
        return opportunities
