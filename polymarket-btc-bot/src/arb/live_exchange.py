"""
Live CLOB execution for structural-arb legs (FOK/FAK taker only).

Uses py-clob-client with credentials from `build_live_clob_client`.
Negative-risk conversion (NO → YES set) is done on-chain via
NegRiskAdapter.convertPositions() using web3.py (see neg_risk_converter.py).
"""
from __future__ import annotations

import uuid
from copy import replace
from datetime import datetime, timezone

import structlog
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.exceptions import PolyApiException
from py_clob_client.order_builder.constants import BUY, SELL

from ..config import Settings
from ..polymarket.clob_factory import build_live_clob_client
from .exchange import PaperExchange
from .models import ArbEvent, FillRecord, OrderIntent, OrderRecord, utc_now
from .neg_risk_converter import (
    convert_no_to_yes,
    ensure_ctf_approved,
    ensure_usdc_approved,
    question_index_from_id,
)

logger = structlog.get_logger(__name__)


class LiveClobExchange(PaperExchange):
    """
    Mirrors PaperExchange for books/positions/settlement; `place_order` posts to Polymarket CLOB.
    Requires ALLOW_TAKER_EXECUTION and FOK/FAK intents — GTC/maker is not supported for live arb.
    """

    def __init__(self, config: Settings) -> None:
        super().__init__(config)
        self._client = build_live_clob_client(config)
        self._ctf_approved: bool = False
        # Approve USDC to Polymarket exchange contracts at startup (required for any buy orders)
        rpc = (config.polygon_rpc_url or "").strip()
        if rpc:
            try:
                ensure_usdc_approved(
                    rpc_url=rpc,
                    wallet_address=(config.polymarket_wallet_address or "").strip(),
                    private_key=(config.wallet_private_key or "").strip(),
                )
            except Exception as exc:
                logger.warning("live_exchange.usdc_approval_failed", error=str(exc))

    def _ensure_ctf_approved_once(self) -> None:
        if self._ctf_approved:
            return
        rpc = (self._config.polygon_rpc_url or "").strip()
        if not rpc:
            raise RuntimeError("POLYGON_RPC_URL not set — required for on-chain neg-risk conversion")
        wallet = (self._config.polymarket_wallet_address or "").strip()
        pk = (self._config.wallet_private_key or "").strip()
        # Approve NegRiskAdapter as operator on CTF (required for convertPositions)
        ensure_ctf_approved(rpc_url=rpc, wallet_address=wallet, private_key=pk)
        self._ctf_approved = True

    def convert_neg_risk(self, event: ArbEvent, source_market_id: str, size: float) -> list[dict]:
        """
        On-chain neg-risk conversion:
          1. Ensure NegRiskAdapter is approved as CTF operator (once per session).
          2. Call NegRiskAdapter.convertPositions() with the market's negRiskMarketID
             and the question_index of the source outcome.
          3. Update the paper-ledger positions to reflect the YES tokens received.

        The YES token quantities mirror what the PaperExchange would compute.
        """
        # ── validate source market ────────────────────────────────────────────
        source_market = event.market_by_id(source_market_id)
        if source_market is None:
            raise ValueError(f"unknown source market {source_market_id}")

        nr_market_id: str = source_market.raw.get("negRiskMarketID") or ""
        question_id: str = source_market.raw.get("questionID") or ""
        if not nr_market_id or not question_id:
            raise RuntimeError(
                f"market {source_market_id} missing negRiskMarketID/questionID in raw data; "
                "cannot perform on-chain conversion"
            )

        question_index = question_index_from_id(question_id)

        # ── one-time CTF approval ─────────────────────────────────────────────
        self._ensure_ctf_approved_once()

        # ── on-chain conversion ───────────────────────────────────────────────
        rpc = (self._config.polygon_rpc_url or "").strip()
        tx_hash = convert_no_to_yes(
            rpc_url=rpc,
            wallet_address=(self._config.polymarket_wallet_address or "").strip(),
            private_key=(self._config.wallet_private_key or "").strip(),
            neg_risk_market_id=nr_market_id,
            question_index=question_index,
            amount_shares=size,
        )

        logger.info(
            "live_exchange.neg_risk_converted",
            source_market_id=source_market_id,
            question_index=question_index,
            size=size,
            tx=tx_hash,
        )

        # ── update paper ledger (mirrors PaperExchange behaviour) ─────────────
        return super().convert_neg_risk(event, source_market_id, size)

    def place_order(self, intent: OrderIntent) -> tuple[OrderRecord, list[FillRecord]]:
        if intent.order_type == "gtc":
            raise RuntimeError(
                "Live arb does not support GTC/maker legs yet. Set ALLOW_TAKER_EXECUTION=true "
                "so legs use FOK (see engine._place_leg)."
            )

        now = utc_now()
        order_id = f"live-{uuid.uuid4().hex[:10]}"

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

        side_const = BUY if intent.side == "BUY" else SELL
        ot = OrderType.FOK if intent.order_type == "fok" else OrderType.FAK

        import math as _math

        # Polymarket CLOB enforces:
        #   - price: max 2 decimal places (tick = $0.01)
        #   - size: max 5 decimal places
        #   - price × size must be a multiple of $0.01 (USDC maker-amount constraint)
        raw_price = float(intent.price)
        raw_size = float(intent.size)

        # BUY: ceil price so we don't miss the ask; SELL: floor so we don't undershoot bid
        if intent.side == "BUY":
            clob_price = _math.ceil(raw_price * 100) / 100
        else:
            clob_price = _math.floor(raw_price * 100) / 100

        if clob_price <= 0:
            order.status = "rejected"
            order.reason = f"price rounds to zero ({raw_price})"
            self._orders[order.order_id] = order
            return replace(order), []

        # Adjust size so that price × size is a multiple of $0.01
        maker_usdc = round(clob_price * raw_size, 2)  # nearest $0.01
        if maker_usdc <= 0:
            order.status = "rejected"
            order.reason = f"order value < $0.01 (price={clob_price}, size={raw_size})"
            self._orders[order.order_id] = order
            return replace(order), []
        clob_size = round(maker_usdc / clob_price, 5)

        try:
            signed = self._client.create_order(
                OrderArgs(
                    token_id=intent.token_id,
                    price=clob_price,
                    size=clob_size,
                    side=side_const,
                ),
                options=None,
            )
            resp = self._client.post_order(signed, ot)
        except PolyApiException as exc:
            err = getattr(exc, "error_msg", str(exc))
            logger.warning("live_exchange.post_failed", error=str(err))
            order.status = "rejected"
            order.reason = f"clob_api: {err!s}"[:500]
            self._orders[order.order_id] = order
            return replace(order), []
        except Exception as exc:
            logger.error("live_exchange.post_error", error=str(exc))
            order.status = "rejected"
            order.reason = str(exc)[:500]
            self._orders[order.order_id] = order
            return replace(order), []

        if not isinstance(resp, dict):
            order.status = "rejected"
            order.reason = "unexpected clob response"
            self._orders[order.order_id] = order
            return replace(order), []

        err = (resp.get("errorMsg") or "").strip()
        if not resp.get("success"):
            order.status = "rejected"
            order.reason = err or "clob rejected"
            order.metadata["clob_response"] = resp
            self._orders[order.order_id] = order
            return replace(order), []

        if err:
            order.status = "rejected"
            order.reason = err
            order.metadata["clob_response"] = resp
            self._orders[order.order_id] = order
            return replace(order), []

        status = (resp.get("status") or "").lower()
        if status and status not in ("matched",):
            order.status = "rejected"
            order.reason = f"clob status {status!r} (expected matched for immediate fill)"
            order.metadata["clob_response"] = resp
            self._orders[order.order_id] = order
            return replace(order), []

        oid = str(resp.get("orderID") or resp.get("order_id") or order_id)
        order.order_id = oid
        order.filled_size = float(intent.size)
        order.average_price = float(intent.price)
        order.status = "filled"
        order.updated_at = datetime.now(timezone.utc)
        order.metadata["clob_response"] = {k: resp[k] for k in ("status", "orderID", "transactionsHashes", "tradeIDs") if k in resp}

        fill = FillRecord(
            fill_id=f"fill-{uuid.uuid4().hex[:10]}",
            order_id=oid,
            token_id=intent.token_id,
            market_id=intent.market_id,
            event_id=self._token_meta[intent.token_id]["event_id"],
            side=intent.side,
            price=float(intent.price),
            size=float(intent.size),
            fee_paid=0.0,
            rebate_earned=0.0,
            timestamp=utc_now(),
        )

        spread_saved = float(self._config.paper_spread_penalty_bps)
        try:
            self._config.paper_spread_penalty_bps = 0.0
            self._apply_fill(order, fill)
        finally:
            self._config.paper_spread_penalty_bps = spread_saved

        self._orders[order.order_id] = order
        return replace(order), [replace(fill)]
