"""
Live CLOB execution for structural-arb legs (FOK/FAK taker only).

Uses py-clob-client with credentials from `build_live_clob_client`.
Negative-risk conversion (NO → YES set) is done on-chain via
NegRiskAdapter.convertPositions() using web3.py (see neg_risk_converter.py).
"""
from __future__ import annotations

import time
import uuid
from copy import replace
from datetime import datetime, timezone

import structlog
from py_clob_client.clob_types import CreateOrderOptions, OrderArgs, OrderType
from py_clob_client.exceptions import PolyApiException
from py_clob_client.order_builder.constants import BUY, SELL

from ..config import Settings
from ..polymarket.clob_factory import build_live_clob_client
from .exchange import PaperExchange
from .models import ArbEvent, FillRecord, OrderIntent, OrderRecord, PositionRecord, utc_now
from .neg_risk_converter import (
    convert_no_to_yes,
    ensure_ctf_approved,
    ensure_usdc_approved,
    question_index_from_id,
)

logger = structlog.get_logger(__name__)
_POSITION_DUST_SHARES = 0.01


class LiveClobExchange(PaperExchange):
    """
    Mirrors PaperExchange for books/positions/settlement; `place_order` posts to Polymarket CLOB.
    Requires ALLOW_TAKER_EXECUTION and FOK/FAK intents — GTC/maker is not supported for live arb.
    """

    def __init__(self, config: Settings) -> None:
        super().__init__(config)
        self._client = build_live_clob_client(config)
        self._ctf_approved: bool = False
        # Last successful CLOB collateral read (USDC, spendable on exchange) — for /summary UI.
        self.last_clob_collateral_usdc: float | None = None
        # Monotonic clock: last time sync_cash_from_clob_collateral completed a successful API read.
        self._last_clob_refresh_mono: float = 0.0
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

    def _fetch_clob_collateral_usdc(self) -> tuple[float, int, int] | None:
        """
        Match scripts/check_live_connections.py: COLLATERAL balance across signature_type 0/1/2, pick largest.
        Returns (usdc, signature_type, successful_signature_reads) or None on failure.
        """
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            client = self._client
            for sig in (0, 1, 2):
                try:
                    client.update_balance_allowance(
                        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig)
                    )
                except Exception:
                    pass
            best_bal: dict | None = None
            best_sig = -1
            max_balance_raw = -1
            ok_reads = 0
            for sig in (0, 1, 2):
                try:
                    b = client.get_balance_allowance(
                        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig)
                    )
                    if not isinstance(b, dict):
                        continue
                    ok_reads += 1
                    br = int(b.get("balance", 0) or 0)
                    if br > max_balance_raw:
                        max_balance_raw = br
                        best_bal = b
                        best_sig = sig
                except Exception:
                    continue
            if best_bal is None or max_balance_raw < 0:
                return None
            usdc = int(best_bal.get("balance", 0)) / 1e6
            return (float(usdc), int(best_sig), int(ok_reads))
        except Exception as exc:
            logger.warning("live_exchange.collateral_fetch_failed", error=str(exc))
            return None

    def sync_cash_from_clob_collateral(self) -> float | None:
        """
        Align internal `cash` with Polymarket-reported spendable collateral so redeems/deposits off-bot
        are visible to risk and the dashboard. Preserves reserved-cash locks: cash = clob_free + reserved.
        Does not change `contributed_capital` (CLOB cash moves include PnL, redeems, and sync noise —
        contributed stays INITIAL_BANKROLL + recorded deposits).
        """
        fetched = self._fetch_clob_collateral_usdc()
        if fetched is None:
            return None
        clob_free, sig, ok_reads = fetched
        old_cash = float(self.cash)
        prev_clob_free = self.last_clob_collateral_usdc
        reserved = sum(self._reserved_cash.values())
        # Startup / transient guard: if only a subset of signature types answered and the max is 0,
        # avoid snapping cash to zero on a likely partial read (prevents false drawdown halts).
        if clob_free <= 0.0 and old_cash > 1.0 and ok_reads < 3:
            logger.warning(
                "live_exchange.cash_sync_inconclusive_zero",
                clob_free_usdc=round(clob_free, 4),
                signature_type=sig,
                successful_signature_reads=ok_reads,
                old_cash=round(old_cash, 4),
                previous_clob_free_usdc=(
                    round(float(prev_clob_free), 4) if isinstance(prev_clob_free, (int, float)) else None
                ),
            )
            self._last_clob_refresh_mono = time.monotonic()
            # Keep the last known good collateral snapshot for UI/risk until a conclusive refresh lands.
            return float(prev_clob_free) if isinstance(prev_clob_free, (int, float)) else clob_free
        self.last_clob_collateral_usdc = clob_free
        new_cash = float(clob_free) + reserved
        delta = new_cash - old_cash
        # Always align ledger to CLOB + reservations when the API responds (fixes drift and FP noise).
        self.cash = new_cash
        self._last_clob_refresh_mono = time.monotonic()
        # Do not bump contributed_capital on cash increases: redeems, PnL settling to cash, and
        # sync corrections are not new contributions (those are INITIAL_BANKROLL + deposit rows).
        if abs(delta) > 1e-4:
            logger.info(
                "live_exchange.cash_synced_from_clob",
                clob_free_usdc=round(clob_free, 4),
                signature_type=sig,
                reserved_usdc=round(reserved, 4),
                delta_cash=round(delta, 4),
                new_cash=round(self.cash, 4),
            )
        return clob_free

    def sync_positions_from_clob_trades(self) -> int:
        """
        Rebuild current holdings by querying CLOB conditional-token balances for traded assets.

        Trade history is used to discover candidate token ids and metadata; authoritative size
        comes from `get_balance_allowance(asset_type=CONDITIONAL, token_id=...)`.
        """
        try:
            trades = self._client.get_trades()
        except Exception as exc:
            logger.warning("live_exchange.positions_sync_failed", error=str(exc))
            return 0

        rows: list[dict] = []
        if isinstance(trades, list):
            rows = [t for t in trades if isinstance(t, dict)]
        elif isinstance(trades, dict):
            maybe = trades.get("data") or trades.get("trades") or []
            if isinstance(maybe, list):
                rows = [t for t in maybe if isinstance(t, dict)]
        if not rows:
            return 0

        # Per-token metadata and rough avg-price estimate from trade history.
        est: dict[str, dict[str, float | str]] = {}
        ordered_tokens: list[str] = []
        seen_tokens: set[str] = set()
        # API returns newest first.
        candidate_cap = 500
        for t in reversed(rows):
            if str(t.get("status", "")).upper() != "CONFIRMED":
                continue
            token_id = str(t.get("asset_id") or t.get("token_id") or "").strip()
            if not token_id:
                continue
            if token_id not in seen_tokens:
                seen_tokens.add(token_id)
                ordered_tokens.append(token_id)
                if len(ordered_tokens) >= candidate_cap:
                    # Enough candidates for practical open-position reconstruction.
                    pass
            side = str(t.get("side", "")).upper()
            try:
                size = float(t.get("size") or 0.0)
                price = float(t.get("price") or 0.0)
            except Exception:
                continue
            if size <= 1e-12:
                continue
            rec = est.setdefault(
                token_id,
                {
                    "buy_size": 0.0,
                    "avg_price": 0.0,
                    "market_id": str(t.get("market") or ""),
                    "outcome_name": str(t.get("outcome") or ""),
                },
            )
            cur = float(rec["buy_size"])
            avg = float(rec["avg_price"])
            if side == "BUY":
                total = cur + size
                if total > 1e-12:
                    rec["avg_price"] = ((cur * avg) + (size * price)) / total
                    rec["buy_size"] = total

        # Query authoritative conditional-token balances for candidate assets.
        bal_by_token: dict[str, float] = {}
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        for token_id in ordered_tokens[:candidate_cap]:
            best_raw = -1
            for sig in (2, 1, 0):
                try:
                    b = self._client.get_balance_allowance(
                        BalanceAllowanceParams(
                            asset_type=AssetType.CONDITIONAL,
                            token_id=token_id,
                            signature_type=sig,
                        )
                    )
                    if not isinstance(b, dict):
                        continue
                    br = int(b.get("balance", 0) or 0)
                    if br > best_raw:
                        best_raw = br
                except Exception:
                    continue
            if best_raw > 0:
                # CLOB conditional balances use 6 decimals.
                bal = float(best_raw) / 1e6
                if bal >= _POSITION_DUST_SHARES:
                    bal_by_token[token_id] = bal

        rebuilt: dict[str, PositionRecord] = {}
        for token_id, sz in bal_by_token.items():
            if sz <= 1e-9:
                continue
            r = est.get(token_id, {})
            meta = self._token_meta.get(token_id, {})
            market_id = str(meta.get("market_id") or r.get("market_id") or "")
            event_id = str(meta.get("event_id") or (f"external:{market_id}" if market_id else "external"))
            outcome_name = str(meta.get("outcome_name") or r.get("outcome_name") or "Unknown")
            contract_side = str(meta.get("contract_side") or ("YES" if outcome_name.lower() == "yes" else "NO"))
            rebuilt[token_id] = PositionRecord(
                token_id=token_id,
                market_id=market_id,
                event_id=event_id,
                outcome_name=outcome_name,
                contract_side=contract_side,  # type: ignore[arg-type]
                size=sz,
                avg_price=max(0.0, float(r["avg_price"])),
                updated_at=utc_now(),
            )

        self._positions = rebuilt
        logger.info(
            "live_exchange.positions_synced",
            positions=len(rebuilt),
            candidate_tokens=min(len(ordered_tokens), candidate_cap),
            balances_positive=len(bal_by_token),
            trades_seen=len(rows),
        )
        return len(rebuilt)

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
        if not self._config.neg_risk_live_onchain_available():
            raise RuntimeError(
                "Live neg-risk conversion is disabled for Gnosis Safe (CLOB_SIGNATURE_TYPE=2): "
                "on-chain convertPositions must be sent from the Safe or Polymarket relayer, "
                "not a raw EOA-signed tx. Set ARB_ALLOW_NEG_RISK_LIVE_WITH_SAFE=true only after "
                "wiring py-builder-relayer-client, or use ARB_STRATEGY_MODE=complete_set, or an EOA wallet."
            )
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

        # Polymarket CLOB enforces (per exchange contract validation):
        #   - price:  max 2 decimal places (tick = $0.01)
        #   - maker_amount (USDC spend): max 2 decimal places → price × size must be
        #     a multiple of $0.01
        #   - taker_amount (outcome tokens): max 4 decimal places
        #
        # For price p (in 2dp) and size s (in 2dp), p×s has at most 4dp.
        # We need p×s to land on exactly 2dp.  This requires s to be a multiple of
        # 0.01 × (100 / gcd(price_cents, 100)).
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

        # Compute minimum size step so that clob_price × size is always a $0.01 multiple.
        # step = 0.01 × ⌈100 / gcd(price_cents, 100)⌉
        price_cents = round(clob_price * 100)  # integer 1..99
        step = 0.01 * (100 // _math.gcd(price_cents, 100))
        clob_size = _math.floor(raw_size / step) * step
        clob_size = round(clob_size, 2)

        maker_usdc = round(clob_price * clob_size, 2)
        if maker_usdc <= 0 or clob_size <= 0:
            order.status = "rejected"
            order.reason = f"order value < $0.01 (price={clob_price}, size={raw_size})"
            self._orders[order.order_id] = order
            return replace(order), []

        # Pass neg_risk and tick_size explicitly so the SDK routes to the correct exchange
        # contract and validates price precision without an extra network round-trip.
        token_meta = self._token_meta.get(intent.token_id, {})
        is_neg_risk = token_meta.get("neg_risk") == "true"
        tick_size = token_meta.get("tick_size", "0.01")
        order_opts = CreateOrderOptions(neg_risk=is_neg_risk, tick_size=tick_size)

        try:
            signed = self._client.create_order(
                OrderArgs(
                    token_id=intent.token_id,
                    price=clob_price,
                    size=clob_size,
                    side=side_const,
                ),
                options=order_opts,
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
