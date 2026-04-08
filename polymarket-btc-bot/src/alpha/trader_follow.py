"""
Optional sleeve: mirror recent public trades from Polymarket Data API leaderboard wallets.

Uses https://data-api.polymarket.com/v1/leaderboard and /trades (no auth). Matches `asset`
(token id) to the current Gamma/CLOB universe, then places a scaled FOK/FAK taker order.

**Default: disabled.** With live structural arb, enable only after reading risks below.

Past PnL is not predictive; leaders may be lucky, illiquid, or trading information you do not have.
Slippage vs their fill is guaranteed. Prefer paper mode or TRADER_FOLLOW_ALLOW_LIVE=false until tuned.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

import aiohttp
import structlog

from ..arb.models import ArbEvent, OrderIntent, TokenBook

logger = structlog.get_logger(__name__)

UA = "Mozilla/5.0 (compatible; polymarket-btc-bot/trader-follow; +https://github.com)"
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=35.0)


def resolve_token_in_universe(
    asset: str, events: list[ArbEvent]
) -> tuple[ArbEvent, Any, Literal["YES", "NO"]] | None:
    """Map CLOB outcome token id to (event, market, YES|NO position side)."""
    aid = str(asset).strip()
    if not aid:
        return None
    for ev in events:
        for m in ev.markets:
            if str(m.yes_token_id).strip() == aid:
                return ev, m, "YES"
            if str(m.no_token_id).strip() == aid:
                return ev, m, "NO"
    return None


def _dedupe_key(trade: dict[str, Any], wallet: str) -> str:
    tx = str(trade.get("transactionHash") or "").strip()
    if tx:
        return tx
    return (
        f"{wallet.lower()}|{trade.get('asset')}|{trade.get('timestamp')}|"
        f"{trade.get('side')}|{trade.get('size')}"
    )


async def run_trader_follow(
    engine: Any,
    events: list[ArbEvent],
    books: dict[str, TokenBook],
    real_books: dict[str, TokenBook],
    opportunities_count: int,
    arb_executed: int,
) -> None:
    cfg = engine._config
    if not cfg.enable_trader_follow:
        return
    if engine._risk.halted or not cfg.allow_taker_execution:
        return

    paper_ok = bool(cfg.paper_trade)
    live_copy_ok = bool(cfg.trader_follow_allow_live) and bool(cfg.arb_live_execution)
    if not paper_ok and not live_copy_ok:
        return

    engine._trader_follow_cycle_idx = getattr(engine, "_trader_follow_cycle_idx", 0) + 1
    if engine._trader_follow_cycle_idx % max(1, int(cfg.trader_follow_every_n_cycles)) != 0:
        return

    if cfg.trader_follow_only_when_no_arb and (opportunities_count > 0 or arb_executed > 0):
        return

    base = str(cfg.trader_follow_data_api_base or "").strip().rstrip("/")
    if not base:
        return

    max_age = max(60, int(cfg.trader_follow_max_trade_age_seconds))
    max_copies = max(1, int(cfg.trader_follow_max_copies_per_cycle))
    max_notional = float(cfg.trader_follow_max_notional)
    min_leader = float(cfg.trader_follow_min_leader_notional)
    frac = max(0.0, float(cfg.trader_follow_size_fraction))
    max_spread = float(cfg.trader_follow_max_book_spread)
    cash_floor = float(cfg.trader_follow_cash_floor)
    min_copy = max(0.5, float(cfg.trader_follow_min_copy_notional_usd))
    top_n = max(1, min(50, int(cfg.trader_follow_top_wallets)))
    per_wallet = max(1, min(200, int(cfg.trader_follow_trades_per_wallet)))

    wallets: list[str] = []
    extra = str(cfg.trader_follow_wallets_extra or "").strip()
    if extra:
        for part in extra.split(","):
            w = part.strip().lower()
            if w.startswith("0x") and len(w) == 42:
                wallets.append(w)

    now_ts = time.time()
    copies = 0

    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT, headers={"User-Agent": UA}) as session:
        if len(wallets) < top_n:
            try:
                lb_url = f"{base}/v1/leaderboard"
                params = {
                    "category": str(cfg.trader_follow_leaderboard_category or "OVERALL"),
                    "timePeriod": str(cfg.trader_follow_time_period or "WEEK"),
                    "orderBy": str(cfg.trader_follow_order_by or "PNL"),
                    "limit": str(top_n),
                }
                async with session.get(lb_url, params=params) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "trader_follow.leaderboard_http",
                            status=resp.status,
                            body=(await resp.text())[:200],
                        )
                    else:
                        rows = await resp.json()
                        if isinstance(rows, list):
                            for row in rows:
                                pw = str(row.get("proxyWallet") or "").strip().lower()
                                if pw.startswith("0x") and len(pw) == 42:
                                    wallets.append(pw)
            except Exception as exc:
                logger.warning("trader_follow.leaderboard_error", error=str(exc))

        # De-dupe wallet list while preserving order
        seen_w: set[str] = set()
        uniq_wallets: list[str] = []
        for w in wallets:
            if w not in seen_w:
                seen_w.add(w)
                uniq_wallets.append(w)
        uniq_wallets = uniq_wallets[:top_n]

        if not uniq_wallets:
            logger.info("trader_follow.no_wallets")
            return

        merged: list[tuple[str, dict[str, Any]]] = []
        for w in uniq_wallets:
            try:
                tr_url = f"{base}/trades"
                params = {"user": w, "limit": str(per_wallet)}
                async with session.get(tr_url, params=params) as resp:
                    if resp.status != 200:
                        continue
                    arr = await resp.json()
                    if not isinstance(arr, list):
                        continue
                    for tr in arr:
                        if isinstance(tr, dict):
                            merged.append((w, tr))
            except Exception as exc:
                logger.warning("trader_follow.trades_error", wallet=w[:10], error=str(exc))

        merged.sort(key=lambda x: int(x[1].get("timestamp") or 0), reverse=True)

        for leader_wallet, tr in merged:
            if copies >= max_copies:
                break
            ts = int(tr.get("timestamp") or 0)
            if ts <= 0 or now_ts - float(ts) > max_age:
                continue

            side = str(tr.get("side") or "").upper()
            if side not in {"BUY", "SELL"}:
                continue

            asset = str(tr.get("asset") or "").strip()
            if not asset:
                continue

            try:
                size_l = float(tr.get("size") or 0.0)
                price_l = float(tr.get("price") or 0.0)
            except (TypeError, ValueError):
                continue
            if size_l <= 0 or price_l <= 0:
                continue

            leader_notional = size_l * price_l
            if leader_notional < min_leader:
                continue

            dk = _dedupe_key(tr, leader_wallet)
            if await engine._repository.trader_follow_seen(dk):
                continue

            resolved = resolve_token_in_universe(asset, events)
            if resolved is None:
                continue
            event, market, pos_side = resolved

            book = real_books.get(asset)
            if book is None or book.source != "clob":
                continue
            if book.best_bid <= 0 or book.best_ask <= 0:
                continue
            if (book.best_ask - book.best_bid) > max_spread:
                continue

            if side == "BUY":
                px = float(book.best_ask)
                depth = book.available_to_buy(px)
                raw_sz = size_l * frac
                sz_cap = max_notional / max(px, 1e-6)
                size = min(raw_sz, depth, sz_cap)
                ot = "fok"
                mo = "taker"
            else:
                px = float(book.best_bid)
                depth = book.available_to_sell(px)
                pos = next((p for p in engine._exchange.get_positions() if p.token_id == asset), None)
                if pos is None or pos.size <= 1e-9:
                    continue
                raw_sz = size_l * frac
                sz_cap = max_notional / max(px, 1e-6)
                size = min(raw_sz, depth, pos.size, sz_cap)
                ot = "fak"
                mo = "taker"

            if size < 1e-6:
                continue
            if size * px < min_copy:
                continue

            est = size * px * (1.02 if side == "BUY" else 1.0)
            if engine._exchange.available_cash < cash_floor + est:
                continue

            await engine._repository.record_trader_follow_seen(
                tx_hash=dk,
                leader_wallet=leader_wallet,
                token_id=asset,
                side=side,
            )

            intent = OrderIntent(
                basket_id=f"tfollow-{uuid.uuid4().hex[:10]}",
                opportunity_id="trader-follow",
                token_id=asset,
                market_id=market.market_id,
                event_id=event.event_id,
                contract_side=pos_side,
                side=side,
                price=px,
                size=size,
                order_type=ot,
                maker_or_taker=mo,
                fees_enabled=bool(market.fees_enabled),
                metadata={
                    "trader_follow": True,
                    "leader_wallet": leader_wallet,
                    "leader_tx": str(tr.get("transactionHash") or ""),
                    "leader_title": str(tr.get("title") or "")[:120],
                    "scaled_from_leader_size": size_l,
                    "live_copy": live_copy_ok,
                },
            )
            order, fills = engine._exchange.place_order(intent)
            await engine._repository.record_order(order)
            for fill in fills:
                await engine._repository.record_fill(fill)

            copies += 1
            logger.info(
                "trader_follow.order",
                leader=leader_wallet[:10],
                side=side,
                token=asset[:16],
                size=round(size, 4),
                price=round(px, 4),
                status=order.status,
                filled=round(order.filled_size, 4),
            )
