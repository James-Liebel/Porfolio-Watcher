"""Per-market WindowState, updated every 500 ms from the price aggregator."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from .scanner import ActiveMarket


class WindowStatus(enum.Enum):
    MONITORING = "MONITORING"
    SIGNAL_FOUND = "SIGNAL_FOUND"
    ORDER_PLACED = "ORDER_PLACED"
    FILLED = "FILLED"
    NOT_FILLED = "NOT_FILLED"
    SETTLED_WIN = "SETTLED_WIN"
    SETTLED_LOSS = "SETTLED_LOSS"
    SKIPPED = "SKIPPED"


@dataclass
class WindowState:
    market_id: str
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    start_time: datetime
    end_time: datetime
    asset: str = field(default="BTC")  # "BTC", "ETH", "SOL", "XRP"

    window_open_price: Optional[Decimal] = field(default=None)
    current_yes_price: Decimal = field(default=Decimal("0.5"))
    current_no_price: Decimal = field(default=Decimal("0.5"))
    liquidity_yes: float = field(default=0.0)
    liquidity_no: float = field(default=0.0)
    status: WindowStatus = field(default=WindowStatus.MONITORING)
    seconds_remaining: float = field(default=0.0)

    @classmethod
    def from_market(cls, market: ActiveMarket) -> "WindowState":
        return cls(
            market_id=market.market_id,
            condition_id=market.condition_id,
            question=market.question,
            yes_token_id=market.yes_token_id,
            no_token_id=market.no_token_id,
            start_time=market.start_time,
            end_time=market.end_time,
            current_yes_price=market.current_yes_price,
            current_no_price=market.current_no_price,
            asset=market.asset,
        )

    def update(
        self,
        current_price: Optional[Decimal],
        yes_price: Optional[Decimal] = None,
        no_price: Optional[Decimal] = None,
    ) -> None:
        """Refresh time remaining, window_open_price, and live odds."""
        now = datetime.now(timezone.utc)
        self.seconds_remaining = max(0.0, (self.end_time - now).total_seconds())

        if current_price is not None and self.window_open_price is None:
            self.window_open_price = current_price

        if yes_price is not None:
            self.current_yes_price = yes_price
        if no_price is not None:
            self.current_no_price = no_price

    @property
    def is_active(self) -> bool:
        return self.seconds_remaining > 0

    def in_entry_window(self, entry_secs: int = 30) -> bool:
        return 0 < self.seconds_remaining <= entry_secs
