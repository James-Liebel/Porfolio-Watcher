from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..config import Settings
from .book_matching import filled_size, walk_taker_levels
from .fees import paper_structural_taker_buy_cash, taker_fee_on_notional
from .models import ArbEvent, ArbOpportunity, OpportunityLeg, OutcomeMarket, TokenBook


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


def _book_spread_bps(book: TokenBook) -> float | None:
    mid = book.mid
    if mid <= 1e-12:
        return None
    return (book.spread / mid) * 10000.0


def _books_within_spread_cap(books: list[TokenBook], max_spread_bps: float) -> bool:
    if max_spread_bps <= 0:
        return True
    for b in books:
        sb = _book_spread_bps(b)
        if sb is None:
            return False
        if sb > max_spread_bps + 1e-9:
            return False
    return True


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
            total += paper_structural_taker_buy_cash(
                n,
                fees_enabled=fees_en,
                taker_fee_bps=config.paper_taker_fee_bps,
                spread_penalty_bps=config.paper_spread_penalty_bps,
            )
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
        buy_cost += paper_structural_taker_buy_cash(
            n,
            fees_enabled=buy_fees_en,
            taker_fee_bps=config.paper_taker_fee_bps,
            spread_penalty_bps=config.paper_spread_penalty_bps,
        )

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


@dataclass(slots=True)
class _CompleteSetWork:
    size: float
    cash_out: float
    profit: float
    net_edge_bps: float
    seconds_to_expiry: float | None
    legs_spec: list[tuple[TokenBook, float, bool]]
    yes_templates: list[tuple[float, OpportunityLeg, TokenBook]]


class OpportunityScanner:
    def __init__(self, config: Settings) -> None:
        self._config = config

    def scan(self, events: list[ArbEvent], books: dict[str, TokenBook]) -> list[ArbOpportunity]:
        opportunities: list[ArbOpportunity] = []
        for event in events:
            opportunities.extend(self._complete_set_opportunities(event, books))
            opportunities.extend(self._neg_risk_opportunities(event, books))

        # Rank by absolute expected profit first so long-dated, high-$ arbs are not buried behind
        # small short-dated trades (annualization alone overweights "quick" marginal edges).
        # Tie-break: annualized edge (capital velocity), then spot edge bps, then $/capital.
        opportunities.sort(
            key=lambda opp: (
                opp.expected_profit,
                _annualized_edge_bps(opp.net_edge_bps, opp.seconds_to_expiry),
                opp.net_edge_bps,
                opp.expected_profit / max(opp.capital_required, 1e-9),
            ),
            reverse=True,
        )
        return opportunities[: self._config.max_opportunities_per_cycle * 10]

    def _complete_set_work(self, event: ArbEvent, books: dict[str, TokenBook]) -> _CompleteSetWork | None:
        normalized_outcomes = [_normalize_outcome(market.outcome_name) for market in event.markets]
        if any(not outcome for outcome in normalized_outcomes):
            return None
        if len(set(normalized_outcomes)) != len(normalized_outcomes):
            return None

        legs_spec: list[tuple[TokenBook, float, bool]] = []
        yes_templates: list[tuple[float, OpportunityLeg, TokenBook]] = []
        seconds_to_expiry = _seconds_to_expiry(event.end_time)

        for market in event.markets:
            book = books.get(market.yes_token_id)
            if book is None or book.best_ask <= 0:
                return None
            # Polymarket CLOB enforces 2-decimal price precision (minimum tick = $0.01).
            # Prices below $0.01 cannot be submitted as valid orders.
            if book.best_ask < 0.01:
                return None
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

        leg_books = [b for b, _, _ in legs_spec]
        if not _books_within_spread_cap(leg_books, float(self._config.max_arb_leg_spread_bps)):
            return None

        size, cash_out = _max_size_under_notional_complete_set(
            legs_spec, self._config.max_basket_notional, self._config
        )
        if size <= 1e-12 or cash_out <= 0:
            return None

        payout = size * 1.0
        profit = payout - cash_out
        net_edge_bps = _edge_bps(profit, cash_out)
        return _CompleteSetWork(
            size=size,
            cash_out=cash_out,
            profit=profit,
            net_edge_bps=net_edge_bps,
            seconds_to_expiry=seconds_to_expiry,
            legs_spec=legs_spec,
            yes_templates=yes_templates,
        )

    def _complete_set_opportunities(self, event: ArbEvent, books: dict[str, TokenBook]) -> list[ArbOpportunity]:
        w = self._complete_set_work(event, books)
        if w is None:
            return []
        if w.net_edge_bps < self._config.min_complete_set_edge_bps or w.profit <= 0:
            return []
        min_p = float(self._config.arb_min_expected_profit_usd)
        if min_p > 0 and w.profit < min_p - 1e-12:
            return []

        legs = [
            OpportunityLeg(
                market_id=leg.market_id,
                token_id=leg.token_id,
                outcome_name=leg.outcome_name,
                position_side=leg.position_side,
                action=leg.action,
                price=leg.price,
                size=w.size,
                fees_enabled=leg.fees_enabled,
            )
            for _, leg, _ in w.yes_templates
        ]
        avg_unit_cost = w.cash_out / w.size
        return [
            ArbOpportunity(
                strategy_type="complete_set",
                event_id=event.event_id,
                event_title=event.title,
                gross_edge_bps=_edge_bps(w.size - w.cash_out, w.cash_out),
                net_edge_bps=w.net_edge_bps,
                capital_required=w.cash_out,
                expected_profit=w.profit,
                legs=legs,
                rationale=(
                    f"Buy YES legs (~{avg_unit_cost:.4f}/set incl. fees at size {w.size:.4f}) "
                    f"and settle complete set for 1.0000"
                ),
                requires_conversion=False,
                settle_on_resolution=True,
                cooldown_key=f"complete_set:{event.event_id}",
                seconds_to_expiry=w.seconds_to_expiry,
            )
        ]

    @staticmethod
    def _neg_risk_event_eligible(event: ArbEvent) -> bool:
        if not (event.neg_risk or event.enable_neg_risk):
            return False
        if event.neg_risk_augmented:
            return False
        if any(_is_excluded_neg_risk_outcome(market.outcome_name) for market in event.markets):
            return False
        return True

    def _neg_risk_try_source(
        self, event: ArbEvent, books: dict[str, TokenBook], source_market: OutcomeMarket
    ) -> ArbOpportunity | None:
        no_book = books.get(source_market.no_token_id)
        # Polymarket CLOB requires price >= $0.01 (2 decimal places).
        if no_book is None or no_book.best_ask < 0.01:
            return None

        sell_legs: list[tuple[float, OpportunityLeg, TokenBook]] = []
        sell_specs: list[tuple[TokenBook, float, bool]] = []
        for market in event.markets:
            if market.market_id == source_market.market_id:
                continue
            yes_book = books.get(market.yes_token_id)
            if yes_book is None or yes_book.best_bid < 0.01:
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
            return None

        spread_books = [no_book] + [book for book, _, _ in sell_specs]
        if not _books_within_spread_cap(spread_books, float(self._config.max_arb_leg_spread_bps)):
            return None

        size, buy_cost, sell_in, profit = _max_size_neg_risk_under_notional(
            no_book,
            no_book.best_ask,
            source_market.fees_enabled,
            sell_specs,
            self._config.max_basket_notional,
            self._config,
        )
        if size <= 1e-12 or buy_cost <= 0:
            return None

        net_edge_bps = _edge_bps(profit, buy_cost)
        seconds_to_expiry = _seconds_to_expiry(event.end_time)
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
        return ArbOpportunity(
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

    def _neg_risk_opportunities(self, event: ArbEvent, books: dict[str, TokenBook]) -> list[ArbOpportunity]:
        if not self._neg_risk_event_eligible(event):
            return []

        opportunities: list[ArbOpportunity] = []
        for source_market in event.markets:
            opp = self._neg_risk_try_source(event, books, source_market)
            if opp is None:
                continue
            if opp.net_edge_bps < self._config.min_neg_risk_edge_bps or opp.expected_profit <= 0:
                continue
            min_p = float(self._config.arb_min_expected_profit_usd)
            if min_p > 0 and opp.expected_profit < min_p - 1e-12:
                continue
            opportunities.append(opp)
        return opportunities

    def cycle_diagnostics(self, events: list[ArbEvent], books: dict[str, TokenBook]) -> dict[str, Any]:
        """Structural counts and best raw edges (ignoring MIN_*_EDGE_BPS) for observability.

        Negative max_raw_complete_set_edge_bps means the best priced complete set in the universe still
        costs more than $1/set at top-of-book (after modeled taker fees) — common when markets are tight;
        it is not by itself a bug.
        """
        neg_tagged = 0
        neg_priceable_events = 0
        complete_priceable_events = 0
        best_cs_bps: float | None = None
        best_nr_bps: float | None = None

        for event in events:
            if event.neg_risk or event.enable_neg_risk:
                neg_tagged += 1

            w = self._complete_set_work(event, books)
            if w is not None:
                complete_priceable_events += 1
                best_cs_bps = w.net_edge_bps if best_cs_bps is None else max(best_cs_bps, w.net_edge_bps)

            if self._neg_risk_event_eligible(event):
                raw_edges: list[float] = []
                for source_market in event.markets:
                    opp = self._neg_risk_try_source(event, books, source_market)
                    if opp is not None:
                        raw_edges.append(opp.net_edge_bps)
                if raw_edges:
                    neg_priceable_events += 1
                    mx = max(raw_edges)
                    best_nr_bps = mx if best_nr_bps is None else max(best_nr_bps, mx)

        # Median book spread (bps) across all books that have both bid and ask > 0.
        spreads_bps: list[float] = []
        for book in books.values():
            if book.best_bid > 0 and book.best_ask > 0:
                mid = (book.best_bid + book.best_ask) / 2
                if mid > 1e-9:
                    spreads_bps.append((book.best_ask - book.best_bid) / mid * 10000)
        spreads_bps.sort()
        median_spread_bps: float | None = (
            spreads_bps[len(spreads_bps) // 2] if spreads_bps else None
        )

        return {
            "events_in_universe": len(events),
            "neg_risk_tagged_events": neg_tagged,
            "neg_risk_priceable_events": neg_priceable_events,
            "complete_set_priceable_events": complete_priceable_events,
            "max_raw_complete_set_edge_bps": best_cs_bps,
            "max_raw_neg_risk_edge_bps": best_nr_bps,
            "min_complete_set_edge_bps_config": float(self._config.min_complete_set_edge_bps),
            "min_neg_risk_edge_bps_config": float(self._config.min_neg_risk_edge_bps),
            "median_book_spread_bps": median_spread_bps,
            "max_arb_leg_spread_bps_config": float(self._config.max_arb_leg_spread_bps),
        }
