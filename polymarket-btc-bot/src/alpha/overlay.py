"""
Directional overlay (paper): same predictors as the offline backtest (news + crypto momentum),
run on live Gamma titles + CLOB books. Buys YES only when model vs ask edge is wide enough.

Designed to run *after* structural arb in each cycle: optional, off by default, capped notional,
skips when trading is halted or when arb already found/executed (configurable).
"""
from __future__ import annotations

import asyncio
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp
import structlog

from agents.advisor_settings import AdvisorSettings

from ..arb.models import ArbEvent, OrderIntent, TokenBook
from ..prediction.cases import EventCase
from ..prediction.predictors import predict_history_shrunk, predict_news_keywords, predict_news_llm

logger = structlog.get_logger(__name__)

UA = "Mozilla/5.0 (compatible; polymarket-alpha-overlay/1.0)"
COINGECKO = "https://api.coingecko.com/api/v3"

_rss_cache: dict[str, tuple[float, list[tuple[datetime, str]]]] = {}
_coingecko_cache: dict[str, tuple[float, float | None]] = {}


def _http_get(url: str, timeout: float = 25.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_iso_utc(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _google_news_headlines(query: str, cutoff: datetime, max_items: int = 10) -> list[tuple[datetime, str]]:
    q = " ".join(query.split()[:14])
    enc = urllib.parse.quote(q)
    url = f"https://news.google.com/rss/search?q={enc}&hl=en-US&gl=US&ceid=US:en"
    try:
        raw = _http_get(url, timeout=20.0)
    except OSError:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = root.findall(".//item") + root.findall(".//atom:entry", ns)
    out: list[tuple[datetime, str]] = []
    for it in items:
        title_el = it.find("title")
        title = (title_el.text or "").strip() if title_el is not None else ""
        pub_el = it.find("pubDate")
        if pub_el is None or not pub_el.text:
            dt_el = it.find("atom:updated", ns)
            if dt_el is None or not (dt_el.text or "").strip():
                continue
            try:
                t = _parse_iso_utc(dt_el.text.strip())
            except ValueError:
                continue
        else:
            try:
                t = parsedate_to_datetime(pub_el.text.strip())
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                else:
                    t = t.astimezone(timezone.utc)
            except (TypeError, ValueError):
                continue
        if t >= cutoff:
            continue
        if title:
            out.append((t, title))
        if len(out) >= max_items:
            break
    return out


def _coin_for_title(title: str) -> str | None:
    tl = title.lower()
    if "bitcoin" in tl or "btc" in tl or "satoshi" in tl:
        return "bitcoin"
    if "ethereum" in tl or "ether" in tl or "eth " in tl:
        return "ethereum"
    return None


def _coingecko_log_return(coin_id: str, cutoff: datetime, lookback_days: int) -> float | None:
    key = f"{coin_id}:{int(cutoff.timestamp()) // 3600}:{lookback_days}"
    now = time.monotonic()
    hit = _coingecko_cache.get(key)
    if hit and now - hit[0] < 600.0:
        return hit[1]
    t_end = int(cutoff.timestamp())
    t_start = t_end - (max(1, lookback_days) + 1) * 86400
    url = f"{COINGECKO}/coins/{coin_id}/market_chart/range?vs_currency=usd&from={t_start}&to={t_end}"
    val: float | None = None
    try:
        data = json.loads(_http_get(url, timeout=30.0).decode("utf-8"))
        prices = data.get("prices") or []
        if len(prices) >= 2:
            first = float(prices[0][1])
            last = float(prices[-1][1])
            if first > 0 and last > 0:
                val = max(-2.0, min(2.0, math.log(last / first)))
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        val = None
    _coingecko_cache[key] = (now, val)
    return val


def _cached_headlines(title: str, cutoff: datetime) -> list[tuple[datetime, str]]:
    ck = title[:200]
    now = time.monotonic()
    hit = _rss_cache.get(ck)
    if hit and now - hit[0] < float(900.0):
        return [(t, h) for t, h in hit[1] if t < cutoff]
    rows = _google_news_headlines(title, cutoff)
    _rss_cache[ck] = (now, rows)
    return rows


def _single_binary_market(event: ArbEvent) -> Any | None:
    """One market row with YES/NO tokens (typical Polymarket binary)."""
    if len(event.markets) != 1:
        return None
    m = event.markets[0]
    if not m.yes_token_id or not m.no_token_id:
        return None
    return m


async def run_directional_overlay(
    engine: Any,
    events: list[ArbEvent],
    books: dict[str, TokenBook],
    real_books: dict[str, TokenBook],
    opportunities_count: int,
    arb_executed: int,
) -> None:
    cfg = engine._config
    if not cfg.enable_directional_overlay:
        return
    if engine._risk.halted or not cfg.allow_taker_execution:
        return
    if not cfg.paper_trade:
        return

    engine._overlay_cycle_idx = getattr(engine, "_overlay_cycle_idx", 0) + 1
    if engine._overlay_cycle_idx % max(1, int(cfg.directional_overlay_every_n_cycles)) != 0:
        return

    if cfg.directional_overlay_only_when_no_arb and (opportunities_count > 0 or arb_executed > 0):
        return

    cutoff = datetime.now(timezone.utc)
    candidates: list[tuple[ArbEvent, Any, TokenBook]] = []
    for event in sorted(events, key=lambda e: float(e.liquidity or 0.0), reverse=True):
        m = _single_binary_market(event)
        if m is None:
            continue
        yid = m.yes_token_id
        bk = real_books.get(yid)
        if bk is None or bk.best_ask <= 0 or bk.best_ask >= 0.995:
            continue
        if (bk.best_ask - bk.best_bid) > float(cfg.directional_overlay_max_spread):
            continue
        candidates.append((event, m, bk))
        if len(candidates) >= int(cfg.directional_overlay_max_events_per_cycle):
            break

    shrink = float(cfg.directional_overlay_shrink_weight)
    min_edge = float(cfg.directional_overlay_min_edge)
    max_notional = float(cfg.directional_overlay_max_notional)
    max_contracts = float(cfg.directional_overlay_max_contracts)
    cash_floor = float(cfg.directional_overlay_cash_floor)

    llm_news = bool(cfg.directional_overlay_llm_news)
    advisor_settings = AdvisorSettings() if llm_news else None
    timeout_total = (
        min(120.0, float(advisor_settings.advisor_http_timeout)) if advisor_settings else 60.0
    )
    session: aiohttp.ClientSession | None = None
    try:
        if llm_news and candidates:
            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_total))

        for event, market, yes_book in candidates:
            ask = float(yes_book.best_ask)
            mid = min(0.98, max(0.02, float(yes_book.mid)))
            news_rows = await asyncio.to_thread(_cached_headlines, event.title, cutoff)
            news_jsonl = [
                {"time": t.isoformat(), "headline": h, "body": ""} for t, h in news_rows
            ]

            hist_jsonl: list[dict[str, Any]] = []
            coin = _coin_for_title(event.title)
            if coin:
                sig7 = await asyncio.to_thread(_coingecko_log_return, coin, cutoff, 7)
                sig1 = await asyncio.to_thread(_coingecko_log_return, coin, cutoff, 1)
                t_hist = cutoff.isoformat()
                if sig7 is not None:
                    hist_jsonl.append({"time": t_hist, "metric": "signal_7d", "value": float(sig7)})
                if sig1 is not None:
                    hist_jsonl.append(
                        {"time": t_hist, "metric": "signal_1d", "value": float(sig1)},
                    )

            case = EventCase(
                event_id=event.event_id,
                title=event.title,
                cutoff=cutoff,
                resolved_yes=False,
                market_yes_price=mid,
                news_before=tuple(news_jsonl),
                history_before=tuple(hist_jsonl),
            )
            p_h = predict_history_shrunk(case, market_weight=shrink)
            p_kw = predict_news_keywords(case)
            if llm_news and session is not None and advisor_settings is not None:
                try:
                    p_llm = await predict_news_llm(case, session, advisor_settings)
                    p_n = (p_kw + p_llm) / 2.0
                except Exception as exc:
                    logger.warning(
                        "directional_overlay.llm_news_fallback",
                        event_id=event.event_id,
                        error=str(exc),
                    )
                    p_n = p_kw
            else:
                p_n = p_kw
            p = (p_h + p_n) / 2.0
            edge = p - ask
            if edge < min_edge:
                continue

            max_sh = max_notional / max(ask, 0.01)
            depth = yes_book.available_to_buy(ask)
            size = min(max_sh, depth, max_contracts)
            if size < float(cfg.directional_overlay_min_contracts):
                continue

            est_cost = size * ask * 1.02
            if engine._exchange.cash < cash_floor + est_cost:
                continue

            intent = OrderIntent(
                basket_id=f"dir-ov-{uuid.uuid4().hex[:10]}",
                opportunity_id="directional-overlay",
                token_id=market.yes_token_id,
                market_id=market.market_id,
                event_id=event.event_id,
                contract_side="YES",
                side="BUY",
                price=ask,
                size=size,
                order_type="fok",
                maker_or_taker="taker",
                fees_enabled=bool(market.fees_enabled),
                metadata={
                    "overlay": True,
                    "p_model": p,
                    "edge_vs_ask": edge,
                    "llm_news": llm_news,
                },
            )
            order, _ = engine._exchange.place_order(intent)
            if order.status == "filled" or order.filled_size > 1e-9:
                logger.info(
                    "directional_overlay.filled",
                    event_id=event.event_id,
                    title=event.title[:80],
                    p_model=round(p, 4),
                    yes_ask=round(ask, 4),
                    edge=round(edge, 4),
                    size=order.filled_size,
                    order_id=order.order_id,
                )
            else:
                logger.info(
                    "directional_overlay.skipped_order",
                    event_id=event.event_id,
                    reason=order.reason or order.status,
                    p_model=round(p, 4),
                    edge=round(edge, 4),
                )
            break
    finally:
        if session is not None:
            await session.close()
