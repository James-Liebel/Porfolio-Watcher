from __future__ import annotations

import math
from datetime import datetime, timezone

from ..config import Settings
from .book_matching import filled_size, walk_taker_levels
from .fees import taker_fee_on_notional
from .models import ArbEvent, ArbOpportunity, OpportunityLeg, TokenBook


def _seconds_to_expiry(end_time_str: str) -> float | None:
    """Return seconds until end_time, or None if unparseable / already past."""
    if not end_time_str:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S+00:00",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(end_time_str, fmt).replace(tzinfo=timezone.utc)
            secs = (dt - datetime.now(timezone.utc)).total_seconds()
            return secs if secs > 0 else None
        except ValueError:
            continue
    return None


def _annualized_edge_bps(net_edge_bps: float, seconds_to_expiry: float | None) -> float:
    """Annualize edge over seconds_to_expiry, falling back when expiry is unknown."""
    if not math.isfinite(net_edge_bps):
        return 0.0
    if not seconds_to_expiry or seconds_to_expiry <= 0:
        return net_edge_bps
    annual_seconds = 365.25 * 24 * 3600
    annualized = net_edge_bps * (annual_seconds / seconds_to_expiry)
    return min(annualized, 1_000_000.0)


def _edge_bps(profit: float, capital: float) -> float:
    if capital <= 0:
        return 0.0
    return (profit / capital) * 10000.0


def _complete_set_buy_cash_out(
    legs_spec: list[tuple[TokenBook, float, bool]], size: float, config: Settings
) -> tuple[bool, float]:
    """All legs BUY `size` at each limit; return (all_filled, total_cash_including_taker_fees)."""
    if size <= 1e-12:
        return True, 0.0
    total = 0.0
    for book, limit_price, fees_en in legs_spec:
        ex = walk_taker_levels(book, "BUY", limit_price, size)
        if filled_size(ex) + 1e-11 < size:
            return False, 0.0
        for price, sz in ex:
            n = price * sz
            total += n + taker_fee_on_notional(n, fees_en, config.paper_taker_fee_bps)
    return True, total


def _max_size_under_notional_complete_set(
    legs_spec: list[tuple[TokenBook, float, bool]], max_notional: float, config: Settings
) -> tuple[float, float]:
    """Return (size, cash_out) maximizing size in [0, depth_hi] with cash_out <= max_notional."""
    s_hi = min(book.available_to_buy(lp) for book, lp, _ in legs_spec)
    if s_hi <= 1e-12:
        return 0.0, 0.0

    lo, hi = 0.0, s_hi
    for _ in range(64):
        mid = (lo + hi) * 0.5
        ok, cost = _complete_set_buy_cash_out(legs_spec, mid, config)
        if not ok:
            hi = mid
            continue
        if cost <= max_notional + 1e-9:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break

    ok, final_cost = _complete_set_buy_cash_out(legs_spec, lo, config)
    if not ok or lo <= 1e-12:
        return 0.0, 0.0
    # Clamp numerical dust so risk.max_basket_notional checks stay stable.
    if final_cost > max_notional and final_cost <= max_notional + 1e-4:
        final_cost = float(max_notional)
    return lo, final_cost


def _neg_risk_cashflows(
    no_book: TokenBook,
    no_limit: float,
    buy_fees_en: bool,
    sell_specs: list[tuple[TokenBook, float, bool]],
    size: float,
    config: Settings,
) -> tuple[bool, float, float]:
    """Returns (ok, buy_cash_out, sell_cash_in_net_of_fees)."""
    if size <= 1e-12:
        return True, 0.0, 0.0

    ex_b = walk_taker_levels(no_book, "BUY", no_limit, size)
    if filled_size(ex_b) + 1e-11 < size:
        return False, 0.0, 0.0
    buy_cost = 0.0
    for price, sz in ex_b:
        n = price * sz
        buy_cost += n + taker_fee_on_notional(n, buy_fees_en, config.paper_taker_fee_bps)

    sell_in = 0.0
    for book, lim, fe in sell_specs:
        ex = walk_taker_levels(book, "SELL", lim, size)
        if filled_size(ex) + 1e-11 < size:
            return False, 0.0, 0.0
        for price, sz in ex:
            n = price * sz
            sell_in += n - taker_fee_on_notional(n, fe, config.paper_taker_fee_bps)

    return True, buy_cost, sell_in


def _max_size_neg_risk_under_notional(
    no_book: TokenBook,
    no_limit: float,
    buy_fees_en: bool,
    sell_specs: list[tuple[TokenBook, float, bool]],
    max_notional: float,
    config: Settings,
) -> tuple[float, float, float, float]:
    """Return (size, buy_cost, sell_in, profit) with buy_cost <= max_notional."""
    depth_hi = min(
        [no_book.available_to_buy(no_limit)]
        + [book.available_to_sell(lim) for book, lim, _ in sell_specs]
    )
    if depth_hi <= 1e-12:
        return 0.0, 0.0, 0.0, 0.0

    lo, hi = 0.0, depth_hi
    for _ in range(64):
        mid = (lo + hi) * 0.5
        ok, buy_c, sell_c = _neg_risk_cashflows(no_book, no_limit, buy_fees_en, sell_specs, mid, config)
        if not ok:
            hi = mid
            continue
        if buy_c <= max_notional + 1e-9:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break

    ok, buy_cost, sell_in = _neg_risk_cashflows(no_book, no_limit, buy_fees_en, sell_specs, lo, config)
    if not ok or lo <= 1e-12:
        return 0.0, 0.0, 0.0, 0.0
    if buy_cost > max_notional and buy_cost <= max_notional + 1e-4:
        buy_cost = float(max_notional)
    profit = sell_in - buy_cost
    return lo, buy_cost, sell_in, profit


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

        opportunities.sort(
            key=lambda opp: (
                _annualized_edge_bps(opp.net_edge_bps, opp.seconds_to_expiry),
                opp.expected_profit,
            ),
            reverse=True,
        )
        return opportunities[: self._config.max_opportunities_per_cycle * 10]

    def _complete_set_opportunities(self, event: ArbEvent, books: dict[str, TokenBook]) -> list[ArbOpportunity]:
        normalized_outcomes = [_normalize_outcome(market.outcome_name) for market in event.markets]
        if any(not outcome for outcome in normalized_outcomes):
            return []
        if len(set(normalized_outcomes)) != len(normalized_outcomes):
            return []

        legs_spec: list[tuple[TokenBook, float, bool]] = []
        yes_templates: list[tuple[float, OpportunityLeg, TokenBook]] = []
        seconds_to_expiry = _seconds_to_expiry(event.end_time)

        for market in event.markets:
            book = books.get(market.yes_token_id)
            if book is None or book.best_ask <= 0:
                return []
            legs_spec.append((book, book.best_ask, market.fees_enabled))
            yes_templates.append(
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

        size, cash_out = _max_size_under_notional_complete_set(legs_spec, self._config.max_basket_notional, self._config)
        if size <= 1e-12 or cash_out <= 0:
            return []

        payout = size * 1.0
        profit = payout - cash_out
        net_edge_bps = _edge_bps(profit, cash_out)
        if net_edge_bps < self._config.min_complete_set_edge_bps or profit <= 0:
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
            for _, leg, _ in yes_templates
        ]
        avg_unit_cost = cash_out / size
        return [
            ArbOpportunity(
                strategy_type="complete_set",
                event_id=event.event_id,
                event_title=event.title,
                gross_edge_bps=_edge_bps(size - cash_out, cash_out),
                net_edge_bps=net_edge_bps,
                capital_required=cash_out,
                expected_profit=profit,
                legs=legs,
                rationale=(
                    f"Buy YES legs (~{avg_unit_cost:.4f}/set incl. fees at size {size:.4f}) "
                    f"and settle complete set for 1.0000"
                ),
                requires_conversion=False,
                settle_on_resolution=True,
                cooldown_key=f"complete_set:{event.event_id}",
                seconds_to_expiry=seconds_to_expiry,
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
        seconds_to_expiry = _seconds_to_expiry(event.end_time)
        for source_market in event.markets:
            no_book = books.get(source_market.no_token_id)
            if no_book is None or no_book.best_ask <= 0:
                continue

            sell_legs: list[tuple[float, OpportunityLeg, TokenBook]] = []
            sell_specs: list[tuple[TokenBook, float, bool]] = []
            for market in event.markets:
                if market.market_id == source_market.market_id:
                    continue
                yes_book = books.get(market.yes_token_id)
                if yes_book is None or yes_book.best_bid <= 0:
                    sell_legs = []
                    sell_specs = []
                    break
                sell_specs.append((yes_book, yes_book.best_bid, market.fees_enabled))
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

            size, buy_cost, sell_in, profit = _max_size_neg_risk_under_notional(
                no_book,
                no_book.best_ask,
                source_market.fees_enabled,
                sell_specs,
                self._config.max_basket_notional,
                self._config,
            )
            if size <= 1e-12 or buy_cost <= 0:
                continue

            net_edge_bps = _edge_bps(profit, buy_cost)
            if net_edge_bps < self._config.min_neg_risk_edge_bps or profit <= 0:
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
                    gross_edge_bps=_edge_bps(sell_in - buy_cost, buy_cost),
                    net_edge_bps=net_edge_bps,
                    capital_required=buy_cost,
                    expected_profit=profit,
                    legs=[buy_leg, *sell_legs_sized],
                    rationale=(
                        f"Buy NO on {source_market.outcome_name} at {no_book.best_ask:.4f}, "
                        f"convert, then sell other YES legs (net ~{profit / size:.4f}/unit at size {size:.4f})"
                    ),
                    requires_conversion=True,
                    settle_on_resolution=False,
                    convert_from_market_id=source_market.market_id,
                    convert_to_market_ids=[leg.market_id for leg in sell_legs_sized],
                    cooldown_key=f"neg_risk:{event.event_id}:{source_market.market_id}",
                    seconds_to_expiry=seconds_to_expiry,
                )
            )
        return opportunities
