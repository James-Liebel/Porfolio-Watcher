from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


@dataclass(slots=True)
class PriceLevel:
    price: float
    size: float

    def as_dict(self) -> dict[str, float]:
        return {"price": float(self.price), "size": float(self.size)}


@dataclass(slots=True)
class TokenBook:
    token_id: str
    timestamp: datetime
    best_bid: float
    best_ask: float
    bids: list[PriceLevel] = field(default_factory=list)
    asks: list[PriceLevel] = field(default_factory=list)
    fees_enabled: bool = False
    tick_size: float = 0.01
    source: str = "synthetic"

    @property
    def mid(self) -> float:
        if self.best_bid > 0 and self.best_ask > 0:
            return (self.best_bid + self.best_ask) / 2
        return self.best_bid or self.best_ask

    @property
    def spread(self) -> float:
        if self.best_bid <= 0 or self.best_ask <= 0:
            return 0.0
        return max(self.best_ask - self.best_bid, 0.0)

    def available_to_buy(self, limit_price: float) -> float:
        return sum(level.size for level in self.asks if level.price <= limit_price + 1e-12)

    def available_to_sell(self, limit_price: float) -> float:
        return sum(level.size for level in self.bids if level.price >= limit_price - 1e-12)

    def as_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "timestamp": _iso(self.timestamp),
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "mid": self.mid,
            "spread": self.spread,
            "bids": [level.as_dict() for level in self.bids],
            "asks": [level.as_dict() for level in self.asks],
            "fees_enabled": self.fees_enabled,
            "tick_size": self.tick_size,
            "source": self.source,
        }


@dataclass(slots=True)
class OutcomeMarket:
    event_id: str
    market_id: str
    question: str
    outcome_name: str
    yes_token_id: str
    no_token_id: str
    current_yes_price: float = 0.0
    current_no_price: float = 0.0
    liquidity: float = 0.0
    tick_size: float = 0.01
    fees_enabled: bool = False
    status: str = "active"
    raw: dict[str, Any] = field(default_factory=dict)

    def token_for_position(self, position_side: Literal["YES", "NO"]) -> str:
        return self.yes_token_id if position_side == "YES" else self.no_token_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "market_id": self.market_id,
            "question": self.question,
            "outcome_name": self.outcome_name,
            "yes_token_id": self.yes_token_id,
            "no_token_id": self.no_token_id,
            "current_yes_price": self.current_yes_price,
            "current_no_price": self.current_no_price,
            "liquidity": self.liquidity,
            "tick_size": self.tick_size,
            "fees_enabled": self.fees_enabled,
            "status": self.status,
        }


@dataclass(slots=True)
class ArbEvent:
    event_id: str
    title: str
    category: str = ""
    neg_risk: bool = False
    enable_neg_risk: bool = False
    neg_risk_augmented: bool = False
    status: str = "active"
    liquidity: float = 0.0
    rules_text: str = ""
    end_time: str = ""
    markets: list[OutcomeMarket] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def market_by_id(self, market_id: str) -> OutcomeMarket | None:
        for market in self.markets:
            if market.market_id == market_id:
                return market
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "title": self.title,
            "category": self.category,
            "neg_risk": self.neg_risk,
            "enable_neg_risk": self.enable_neg_risk,
            "neg_risk_augmented": self.neg_risk_augmented,
            "status": self.status,
            "liquidity": self.liquidity,
            "rules_text": self.rules_text,
            "end_time": self.end_time,
            "markets": [market.as_dict() for market in self.markets],
        }


@dataclass(slots=True)
class OpportunityLeg:
    market_id: str
    token_id: str
    outcome_name: str
    position_side: Literal["YES", "NO"]
    action: Literal["BUY", "SELL"]
    price: float
    size: float
    fees_enabled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ArbOpportunity:
    strategy_type: str
    event_id: str
    event_title: str
    gross_edge_bps: float
    net_edge_bps: float
    capital_required: float
    expected_profit: float
    legs: list[OpportunityLeg]
    rationale: str
    requires_conversion: bool = False
    settle_on_resolution: bool = False
    convert_from_market_id: str | None = None
    convert_to_market_ids: list[str] = field(default_factory=list)
    decision: str = "detected"
    reason: str = ""
    cooldown_key: str = ""
    created_at: datetime = field(default_factory=utc_now)
    opportunity_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def as_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "strategy_type": self.strategy_type,
            "event_id": self.event_id,
            "event_title": self.event_title,
            "gross_edge_bps": self.gross_edge_bps,
            "net_edge_bps": self.net_edge_bps,
            "capital_required": self.capital_required,
            "expected_profit": self.expected_profit,
            "rationale": self.rationale,
            "requires_conversion": self.requires_conversion,
            "settle_on_resolution": self.settle_on_resolution,
            "convert_from_market_id": self.convert_from_market_id,
            "convert_to_market_ids": list(self.convert_to_market_ids),
            "decision": self.decision,
            "reason": self.reason,
            "cooldown_key": self.cooldown_key,
            "created_at": _iso(self.created_at),
            "legs": [leg.as_dict() for leg in self.legs],
        }


@dataclass(slots=True)
class OrderIntent:
    basket_id: str
    opportunity_id: str
    token_id: str
    market_id: str
    event_id: str
    contract_side: Literal["YES", "NO"]
    side: Literal["BUY", "SELL"]
    price: float
    size: float
    order_type: Literal["fok", "fak", "gtc"] = "fok"
    maker_or_taker: Literal["maker", "taker"] = "taker"
    fees_enabled: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderRecord:
    order_id: str
    basket_id: str
    opportunity_id: str
    token_id: str
    market_id: str
    side: str
    price: float
    size: float
    order_type: str
    maker_or_taker: str
    status: str
    created_at: datetime
    updated_at: datetime
    filled_size: float = 0.0
    average_price: float = 0.0
    fees_enabled: bool = False
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "basket_id": self.basket_id,
            "opportunity_id": self.opportunity_id,
            "token_id": self.token_id,
            "market_id": self.market_id,
            "side": self.side,
            "price": self.price,
            "size": self.size,
            "order_type": self.order_type,
            "maker_or_taker": self.maker_or_taker,
            "status": self.status,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "filled_size": self.filled_size,
            "average_price": self.average_price,
            "fees_enabled": self.fees_enabled,
            "reason": self.reason,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class FillRecord:
    fill_id: str
    order_id: str
    token_id: str
    market_id: str
    event_id: str
    side: str
    price: float
    size: float
    fee_paid: float
    rebate_earned: float
    timestamp: datetime = field(default_factory=utc_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "token_id": self.token_id,
            "market_id": self.market_id,
            "event_id": self.event_id,
            "side": self.side,
            "price": self.price,
            "size": self.size,
            "fee_paid": self.fee_paid,
            "rebate_earned": self.rebate_earned,
            "timestamp": _iso(self.timestamp),
        }


@dataclass(slots=True)
class PositionRecord:
    token_id: str
    market_id: str
    event_id: str
    outcome_name: str
    contract_side: Literal["YES", "NO"]
    size: float
    avg_price: float
    updated_at: datetime = field(default_factory=utc_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "market_id": self.market_id,
            "event_id": self.event_id,
            "outcome_name": self.outcome_name,
            "contract_side": self.contract_side,
            "size": self.size,
            "avg_price": self.avg_price,
            "updated_at": _iso(self.updated_at),
        }


@dataclass(slots=True)
class BasketRecord:
    basket_id: str
    opportunity_id: str
    event_id: str
    strategy_type: str
    status: str
    capital_reserved: float
    target_net_edge_bps: float
    realized_net_pnl: float = 0.0
    notes: str = ""
    created_at: datetime = field(default_factory=utc_now)
    closed_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "basket_id": self.basket_id,
            "opportunity_id": self.opportunity_id,
            "event_id": self.event_id,
            "strategy_type": self.strategy_type,
            "status": self.status,
            "capital_reserved": self.capital_reserved,
            "target_net_edge_bps": self.target_net_edge_bps,
            "realized_net_pnl": self.realized_net_pnl,
            "notes": self.notes,
            "created_at": _iso(self.created_at),
            "closed_at": _iso(self.closed_at),
        }
