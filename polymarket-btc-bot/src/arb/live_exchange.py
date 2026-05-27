"""Live CLOB execution adapter — real Polymarket orders, behind hard gates.

``LiveClobExchange`` subclasses :class:`PaperExchange` so every bit of accounting
(positions, cash, realized P&L, fees, event exposure, settlement) is shared and
identical to paper. The *only* thing that changes is how a single order gets
filled:

* **Not armed** (the default, and any dry-run / missing-creds state) — the exact
  signed ``OrderArgs`` that *would* be sent is logged, then the order is filled by
  the inherited paper simulation against the live book snapshot. This exercises
  the entire decision + sizing + book-walk path with zero real-money risk.
* **Armed** (``Settings.live_execution_armed()`` is True) — the order is created,
  signed and POSTed to Polymarket as a Fill-or-Kill taker order, and the response
  is reconciled into local inventory.

Negative-risk *conversion* requires an on-chain NegRiskAdapter call that is not
wired here, so when armed those baskets are refused before any capital is
committed (see ``convert_neg_risk`` and the risk-manager gate). Complete-set
baskets are pure CLOB taker orders and execute fully.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

from ..config import Settings
from .exchange import PaperExchange
from .fees import taker_fee_on_notional
from .models import ArbEvent, FillRecord, OrderIntent, OrderRecord, PositionRecord, utc_now

logger = structlog.get_logger(__name__)


class LiveExecutionError(RuntimeError):
    """Raised when a live order cannot be submitted or reconciled safely."""


class LiveClobExchange(PaperExchange):
    def __init__(self, config: Settings, client: Any | None = None) -> None:
        super().__init__(config)
        self._client = client
        self._client_init_attempted = client is not None
        self.execution_mode = "live" if config.live_execution_armed() else "live_dry_run"

    # ── client ───────────────────────────────────────────────────────────

    def _ensure_client(self) -> Any | None:
        if self._client is not None or self._client_init_attempted:
            return self._client
        self._client_init_attempted = True
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            self._client = ClobClient(
                host=self._config.clob_host,
                chain_id=137,
                key=self._config.wallet_private_key,
                creds=ApiCreds(
                    api_key=self._config.polymarket_api_key,
                    api_secret=self._config.polymarket_secret,
                    api_passphrase=self._config.polymarket_passphrase,
                ),
                signature_type=self._config.clob_signature_type,
                funder=self._config.polymarket_wallet_address,
            )
        except Exception as exc:  # pragma: no cover - depends on real creds
            logger.error("live_exchange.client_init_failed", error=str(exc))
            self._client = None
        return self._client

    # ── order placement ──────────────────────────────────────────────────

    def place_order(self, intent: OrderIntent) -> tuple[OrderRecord, list[FillRecord]]:
        notional = float(intent.price) * float(intent.size)
        # Last-line ceiling, independent of basket sizing: never let a single live
        # order exceed this USDC notional no matter what the scanner produced.
        if notional > float(self._config.live_max_order_usdc) + 1e-9:
            order = self._rejected_order(intent, "live_max_order_usdc exceeded")
            logger.error(
                "live_exchange.order_over_ceiling",
                token_id=intent.token_id,
                notional=round(notional, 4),
                ceiling=self._config.live_max_order_usdc,
            )
            return order, []

        if not self._config.live_execution_armed():
            # Dry-run: log the real order, then simulate so local state stays sane.
            logger.info(
                "live_exchange.dry_run_order",
                token_id=intent.token_id,
                side=intent.side,
                price=round(float(intent.price), 6),
                size=round(float(intent.size), 4),
                notional=round(notional, 4),
                order_type=intent.order_type,
            )
            order, fills = super().place_order(intent)
            order.metadata = {**order.metadata, "live_dry_run": True}
            self._orders[order.order_id] = order
            return order, fills

        return self._place_live_order(intent, notional)

    def _place_live_order(self, intent: OrderIntent, notional: float) -> tuple[OrderRecord, list[FillRecord]]:
        client = self._ensure_client()
        if client is None:
            return self._rejected_order(intent, "live client unavailable"), []

        # Fill-or-Kill taker: the whole leg fills at or better than the limit, or
        # nothing does. That matches the scanner's all-or-nothing basket sizing and
        # avoids being left with a partial leg to unwind.
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(
                token_id=intent.token_id,
                price=float(intent.price),
                size=float(intent.size),
                side=intent.side,
            )
            signed = client.create_order(order_args)
            response = client.post_order(signed, OrderType.FOK)
        except Exception as exc:  # pragma: no cover - network / signing
            logger.error("live_exchange.post_failed", token_id=intent.token_id, error=str(exc))
            return self._rejected_order(intent, f"post_failed: {exc}"), []

        filled, fill_size, fill_price, order_id = self._interpret_response(response, intent)
        if not filled:
            logger.warning(
                "live_exchange.order_not_filled",
                token_id=intent.token_id,
                response=str(response)[:300],
            )
            return self._rejected_order(intent, "fok_not_filled", order_id=order_id), []

        order, fill = self._record_live_fill(intent, fill_price, fill_size, order_id)
        logger.info(
            "live_exchange.order_filled",
            order_id=order.order_id,
            token_id=intent.token_id,
            side=intent.side,
            price=round(fill_price, 6),
            size=round(fill_size, 4),
        )
        return order, [fill]

    @staticmethod
    def _interpret_response(response: Any, intent: OrderIntent) -> tuple[bool, float, float, str]:
        """Conservatively decide whether a FOK order actually filled.

        Bias hard toward 'not filled' on anything ambiguous: a phantom fill makes
        us track inventory we don't own (real loss at settlement), whereas treating
        a real fill as a miss only triggers a safe unwind of a position we hold.
        """
        if not isinstance(response, dict):
            return False, 0.0, 0.0, ""
        order_id = str(response.get("orderID") or response.get("orderId") or response.get("id") or "")
        success = bool(response.get("success", False))
        status = str(response.get("status") or response.get("orderStatus") or "").strip().lower()
        error = str(response.get("errorMsg") or response.get("error") or "").strip()
        if error or not success:
            return False, 0.0, 0.0, order_id
        if status not in ("matched", "filled", "success"):
            return False, 0.0, 0.0, order_id
        # FOK that matched filled the full requested size. Trust the limit price as
        # a conservative (never-better-than-actual) cost basis.
        return True, float(intent.size), float(intent.price), order_id

    def _record_live_fill(
        self, intent: OrderIntent, fill_price: float, fill_size: float, order_id: str
    ) -> tuple[OrderRecord, FillRecord]:
        now = utc_now()
        order = OrderRecord(
            order_id=order_id or f"live-{uuid.uuid4().hex[:10]}",
            basket_id=intent.basket_id,
            opportunity_id=intent.opportunity_id,
            token_id=intent.token_id,
            market_id=intent.market_id,
            side=intent.side,
            price=float(intent.price),
            size=float(intent.size),
            order_type=intent.order_type,
            maker_or_taker="taker",
            status="filled",
            created_at=now,
            updated_at=now,
            filled_size=float(fill_size),
            average_price=float(fill_price),
            fees_enabled=intent.fees_enabled,
            metadata={**dict(intent.metadata), "live": True},
        )
        notional = fill_price * fill_size
        fee = taker_fee_on_notional(notional, intent.fees_enabled, self._config.paper_taker_fee_bps)
        meta = self._token_meta.get(intent.token_id, {})

        if intent.side == "BUY":
            self.cash -= notional + fee
            self._merge_position(
                PositionRecord(
                    token_id=intent.token_id,
                    market_id=intent.market_id,
                    event_id=intent.event_id,
                    outcome_name=meta.get("outcome_name", intent.token_id),
                    contract_side=intent.contract_side,
                    size=fill_size,
                    avg_price=fill_price,
                )
            )
        else:
            position = self._positions.get(intent.token_id)
            if position is None or position.size + 1e-12 < fill_size:
                raise LiveExecutionError("live sell fill without sufficient inventory")
            cost_basis = position.avg_price * fill_size
            position.size -= fill_size
            position.updated_at = now
            if position.size <= 1e-12:
                self._positions.pop(intent.token_id, None)
            else:
                self._positions[intent.token_id] = position
            self.cash += notional - fee
            self.realized_pnl += notional - cost_basis

        self.fees_paid += fee
        self.realized_pnl -= fee

        fill = FillRecord(
            fill_id=f"livefill-{uuid.uuid4().hex[:10]}",
            order_id=order.order_id,
            token_id=intent.token_id,
            market_id=intent.market_id,
            event_id=intent.event_id,
            side=intent.side,
            price=fill_price,
            size=fill_size,
            fee_paid=fee,
            rebate_earned=0.0,
        )
        self._orders[order.order_id] = order
        return order, fill

    def _rejected_order(self, intent: OrderIntent, reason: str, order_id: str = "") -> OrderRecord:
        now = utc_now()
        order = OrderRecord(
            order_id=order_id or f"live-{uuid.uuid4().hex[:10]}",
            basket_id=intent.basket_id,
            opportunity_id=intent.opportunity_id,
            token_id=intent.token_id,
            market_id=intent.market_id,
            side=intent.side,
            price=float(intent.price),
            size=float(intent.size),
            order_type=intent.order_type,
            maker_or_taker="taker",
            status="rejected",
            created_at=now,
            updated_at=now,
            fees_enabled=intent.fees_enabled,
            reason=reason,
            metadata=dict(intent.metadata),
        )
        self._orders[order.order_id] = order
        return order

    # ── conversion ───────────────────────────────────────────────────────

    def convert_neg_risk(self, event: ArbEvent, source_market_id: str, size: float) -> list[dict[str, Any]]:
        if self._config.live_execution_armed():
            # The conversion is an on-chain NegRiskAdapter.convertPositions call,
            # which is intentionally not wired into live execution yet. Refuse
            # loudly so the basket fails before legs are committed (the risk gate
            # should already have rejected this opportunity upstream).
            raise LiveExecutionError(
                "live neg-risk conversion is not supported (on-chain adapter not enabled)"
            )
        return super().convert_neg_risk(event, source_market_id, size)
