from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Awaitable, Callable

import aiohttp
import structlog

from ..config import Settings
from .models import ArbEvent, OutcomeMarket
from .pricing import _seconds_to_expiry

logger = structlog.get_logger(__name__)

FetchPayload = Callable[[], Awaitable[dict[str, Any] | tuple[list[dict[str, Any]], list[dict[str, Any]]]]]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _is_resolved_status(value: Any) -> bool:
    return str(value or "").strip().lower() in {
        "resolved",
        "closed",
        "finalized",
        "settled",
        "complete",
    }


def _event_id_from_market_row(row: dict[str, Any]) -> str:
    """Gamma sometimes links markets via eventId; newer payloads use nested event / events[]."""
    for key in ("eventId", "event_id", "parentEventId"):
        raw = row.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    ev = row.get("event")
    if isinstance(ev, dict):
        nested = ev.get("id") or ev.get("eventId")
        if nested is not None and str(nested).strip():
            return str(nested).strip()
    events = row.get("events")
    if isinstance(events, list):
        for item in events:
            if not isinstance(item, dict):
                continue
            nested = item.get("id") or item.get("eventId")
            if nested is not None and str(nested).strip():
                return str(nested).strip()
    return ""


class GammaUniverseService:
    def __init__(
        self,
        config: Settings,
        session: aiohttp.ClientSession | None = None,
        fetch_payload: FetchPayload | None = None,
    ) -> None:
        self._config = config
        self._session = session
        self._fetch_payload = fetch_payload
        self._owns_session = session is None

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def refresh(self) -> list[ArbEvent]:
        event_rows, market_rows = await self._load_payload()
        events = self._build_events(event_rows, market_rows)
        logger.info(
            "universe.refreshed",
            events=len(events),
            markets=sum(len(event.markets) for event in events),
            gamma_event_rows=len(event_rows),
            gamma_market_rows=len(market_rows),
        )
        return events

    async def lookup_resolution(
        self,
        event_id: str,
        fallback_event: ArbEvent | None = None,
    ) -> tuple[ArbEvent | None, str | None, str]:
        meta, market_rows = await self._load_event_payload(event_id)
        event = self._build_event_snapshot(event_id, meta, market_rows, fallback_event)
        resolution_market_id, source = self._infer_resolution_market(meta, market_rows, fallback_event)
        return event, resolution_market_id, source

    async def _load_payload(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if self._fetch_payload is not None:
            payload = await self._fetch_payload()
            if isinstance(payload, tuple):
                return payload
            return payload.get("events", []), payload.get("markets", [])

        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=float(self._config.gamma_http_timeout_seconds))
            self._session = aiohttp.ClientSession(timeout=timeout)

        event_rows = await self._paged_gamma_list(
            "/events",
            {"active": "true", "closed": "false"},
            page_size=max(1, int(self._config.gamma_event_page_size)),
            max_pages=max(1, int(self._config.gamma_event_max_pages)),
        )
        market_rows = await self._paged_gamma_list(
            "/markets",
            {"active": "true", "closed": "false"},
            page_size=max(1, int(self._config.gamma_market_page_size)),
            max_pages=max(1, int(self._config.gamma_market_max_pages)),
        )

        return event_rows, market_rows

    async def _paged_gamma_list(
        self,
        path: str,
        base_params: dict[str, str],
        page_size: int,
        max_pages: int,
    ) -> list[dict[str, Any]]:
        """GET Gamma list endpoints with offset pagination; dedupe by row id."""
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        url = f"{self._config.gamma_base_url}{path}"
        pages_fetched = 0
        for page in range(max_pages):
            params = {
                **base_params,
                "limit": str(page_size),
                "offset": str(page * page_size),
            }
            async with self._session.get(url, params=params) as resp:
                if resp.status >= 400:
                    logger.warning(
                        "universe.gamma_page_error",
                        path=path,
                        status=resp.status,
                        page=page,
                    )
                    break
                chunk = await resp.json()
            pages_fetched += 1
            if not isinstance(chunk, list) or not chunk:
                break
            for row in chunk:
                rid = str(row.get("id") or row.get("conditionId") or "")
                if rid and rid not in seen:
                    seen.add(rid)
                    merged.append(row)
            if len(chunk) < page_size:
                break
        logger.debug(
            "universe.gamma_pages",
            path=path,
            pages=pages_fetched,
            rows=len(merged),
            page_size=page_size,
        )
        return merged

    async def _load_event_payload(self, event_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=float(self._config.gamma_http_timeout_seconds))
            self._session = aiohttp.ClientSession(timeout=timeout)

        meta: dict[str, Any] = {}
        market_rows: list[dict[str, Any]] = []

        event_urls = [
            (f"{self._config.gamma_base_url}/events/{event_id}", None),
            (f"{self._config.gamma_base_url}/events", {"id": event_id, "limit": "1"}),
        ]
        for url, params in event_urls:
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status >= 400:
                        continue
                    payload = await resp.json()
            except Exception:
                continue
            if isinstance(payload, dict):
                meta = payload
                break
            if isinstance(payload, list) and payload:
                meta = payload[0]
                break

        market_param_sets = [
            {"eventId": event_id, "limit": "100"},
            {"event_id": event_id, "limit": "100"},
        ]
        for params in market_param_sets:
            try:
                async with self._session.get(f"{self._config.gamma_base_url}/markets", params=params) as resp:
                    if resp.status >= 400:
                        continue
                    payload = await resp.json()
            except Exception:
                continue
            if isinstance(payload, list) and payload:
                market_rows = payload
                break

        return meta, market_rows

    def _build_events(self, event_rows: list[dict[str, Any]], market_rows: list[dict[str, Any]]) -> list[ArbEvent]:
        event_meta: dict[str, dict[str, Any]] = {}
        for row in event_rows:
            event_id = str(row.get("id") or row.get("eventId") or row.get("event_id") or "")
            if event_id:
                event_meta[event_id] = row

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in market_rows:
            event_id = _event_id_from_market_row(row)
            if event_id:
                grouped[event_id].append(row)

        results: list[ArbEvent] = []
        for event_id, rows in grouped.items():
            meta = event_meta.get(event_id, {})
            markets = [
                market
                for market in (self._parse_market(event_id, row) for row in rows)
                if market is not None
            ]
            if len(markets) < self._config.min_outcomes_per_event:
                continue

            liquidity = _as_float(meta.get("liquidity"), default=sum(market.liquidity for market in markets))
            if liquidity < self._config.min_event_liquidity:
                continue

            title = str(
                meta.get("title")
                or meta.get("name")
                or meta.get("question")
                or rows[0].get("eventTitle")
                or rows[0].get("title")
                or rows[0].get("question")
                or event_id
            )
            status = str(meta.get("status") or rows[0].get("status") or "active").lower()
            if status in {"resolved", "closed", "finalized"}:
                continue
            category_str = str(meta.get("category") or rows[0].get("category") or "")
            if not self._config.category_is_allowed(category_str):
                continue

            end_time = str(meta.get("endDate") or meta.get("end_time") or rows[0].get("endDate") or "")
            if (
                self._config.universe_max_hours_to_resolution > 0
                or self._config.universe_min_hours_to_resolution > 0
            ):
                secs = _seconds_to_expiry(end_time)
                if secs is not None:
                    hours = secs / 3600.0
                    mx = self._config.universe_max_hours_to_resolution
                    mn = self._config.universe_min_hours_to_resolution
                    if mx > 0 and hours > mx:
                        continue
                    if mn > 0 and hours < mn:
                        continue

            results.append(
                ArbEvent(
                    event_id=event_id,
                    title=title,
                    category=str(meta.get("category") or rows[0].get("category") or ""),
                    neg_risk=_as_bool(meta.get("negRisk") or rows[0].get("negRisk")),
                    enable_neg_risk=_as_bool(meta.get("enableNegRisk") or rows[0].get("enableNegRisk")),
                    neg_risk_augmented=_as_bool(meta.get("negRiskAugmented") or rows[0].get("negRiskAugmented")),
                    status=status,
                    liquidity=liquidity,
                    rules_text=str(meta.get("rules") or meta.get("description") or rows[0].get("description") or ""),
                    end_time=end_time,
                    markets=markets,
                    raw=meta or {"event_id": event_id},
                )
            )

        def _soon_key(event: ArbEvent) -> float:
            if not self._config.universe_prefer_shorter_resolution:
                return 0.0
            secs = _seconds_to_expiry(event.end_time)
            if secs is None or secs < 0:
                return -1e15
            return -float(secs)

        def _rank(event: ArbEvent) -> tuple[int | float, ...]:
            sk = _soon_key(event)
            if self._config.universe_prefer_neg_risk:
                nr = (
                    1
                    if (event.neg_risk or event.enable_neg_risk) and not event.neg_risk_augmented
                    else 0
                )
                return (nr, event.liquidity, sk)
            return (event.liquidity, sk)

        results.sort(key=_rank, reverse=True)
        return results[: self._config.max_tracked_events]

    def _build_event_snapshot(
        self,
        event_id: str,
        meta: dict[str, Any],
        market_rows: list[dict[str, Any]],
        fallback_event: ArbEvent | None,
    ) -> ArbEvent | None:
        markets = [
            market
            for market in (self._parse_market(event_id, row) for row in market_rows)
            if market is not None
        ]
        if not markets and fallback_event is not None:
            markets = list(fallback_event.markets)
        if not markets and not meta and fallback_event is None:
            return None

        title = str(
            meta.get("title")
            or meta.get("name")
            or meta.get("question")
            or (fallback_event.title if fallback_event else "")
            or (market_rows[0].get("eventTitle") if market_rows else "")
            or event_id
        )
        category = str(
            meta.get("category")
            or (fallback_event.category if fallback_event else "")
            or (market_rows[0].get("category") if market_rows else "")
        )
        status = str(
            meta.get("status")
            or (fallback_event.status if fallback_event else "")
            or (market_rows[0].get("status") if market_rows else "")
            or "unknown"
        ).lower()
        liquidity = _as_float(
            meta.get("liquidity"),
            default=(fallback_event.liquidity if fallback_event else sum(m.liquidity for m in markets)),
        )
        return ArbEvent(
            event_id=event_id,
            title=title,
            category=category,
            neg_risk=_as_bool(meta.get("negRisk") or (fallback_event.neg_risk if fallback_event else False)),
            enable_neg_risk=_as_bool(meta.get("enableNegRisk") or (fallback_event.enable_neg_risk if fallback_event else False)),
            neg_risk_augmented=_as_bool(meta.get("negRiskAugmented") or (fallback_event.neg_risk_augmented if fallback_event else False)),
            status=status,
            liquidity=liquidity,
            rules_text=str(meta.get("rules") or meta.get("description") or (fallback_event.rules_text if fallback_event else "")),
            end_time=str(meta.get("endDate") or meta.get("end_time") or (fallback_event.end_time if fallback_event else "")),
            markets=markets,
            raw=meta or (fallback_event.raw if fallback_event else {"event_id": event_id}),
        )

    def _infer_resolution_market(
        self,
        meta: dict[str, Any],
        market_rows: list[dict[str, Any]],
        fallback_event: ArbEvent | None,
    ) -> tuple[str | None, str]:
        for key in ("winnerMarketId", "winningMarketId", "resolutionMarketId", "resolvedMarketId"):
            direct_market_id = str(meta.get(key) or "").strip()
            if direct_market_id:
                return direct_market_id, f"event_field:{key}"

        resolved_hint = _as_bool(meta.get("resolved") or meta.get("closed") or meta.get("finalized"))
        resolved_hint = resolved_hint or _is_resolved_status(meta.get("status"))
        resolved_hint = resolved_hint or any(
            _as_bool(row.get("winner") or row.get("isWinner")) or _is_resolved_status(row.get("status"))
            for row in market_rows
        )

        for row in market_rows:
            if _as_bool(row.get("winner") or row.get("isWinner")):
                market_id = str(row.get("id") or row.get("marketId") or row.get("conditionId") or "")
                if market_id:
                    return market_id, "market_flag:winner"

        winning_outcome = str(
            meta.get("winningOutcome")
            or meta.get("resolvedOutcome")
            or meta.get("winner")
            or ""
        ).strip()
        if winning_outcome and not winning_outcome.lower() in {"true", "false"}:
            market_id = self._market_id_for_outcome_name(winning_outcome, market_rows, fallback_event)
            if market_id:
                return market_id, "event_field:winningOutcome"

        if resolved_hint:
            candidates: list[str] = []
            for row in market_rows:
                market_id = str(row.get("id") or row.get("marketId") or row.get("conditionId") or "")
                if not market_id:
                    continue
                price_candidates = [
                    _as_float(row.get("finalYesPrice"), default=-1.0),
                    _as_float(row.get("yesPrice"), default=-1.0),
                    _as_float(row.get("lastTradePrice"), default=-1.0),
                    _as_float(row.get("bestBid"), default=-1.0),
                    _as_float(row.get("bestAsk"), default=-1.0),
                ]
                if max(price_candidates) >= 0.999:
                    candidates.append(market_id)
            if len(candidates) == 1:
                return candidates[0], "resolved_price:1.0"
            if len(candidates) > 1:
                # Multiple markets near 1.0 - pick the one with the highest price.
                best_market_id = None
                best_price = -1.0
                for row in market_rows:
                    market_id = str(row.get("id") or row.get("marketId") or row.get("conditionId") or "")
                    if market_id not in candidates:
                        continue
                    p = max(
                        _as_float(row.get("finalYesPrice"), default=-1.0),
                        _as_float(row.get("yesPrice"), default=-1.0),
                        _as_float(row.get("lastTradePrice"), default=-1.0),
                    )
                    if p > best_price:
                        best_price = p
                        best_market_id = market_id
                if best_market_id:
                    return best_market_id, "resolved_price:highest"

        return None, "unresolved"

    def _market_id_for_outcome_name(
        self,
        outcome_name: str,
        market_rows: list[dict[str, Any]],
        fallback_event: ArbEvent | None,
    ) -> str | None:
        normalized_target = " ".join(outcome_name.strip().lower().split())
        if not normalized_target:
            return None
        for row in market_rows:
            candidate = str(row.get("outcome") or row.get("groupItemTitle") or row.get("title") or "").strip()
            if " ".join(candidate.lower().split()) == normalized_target:
                return str(row.get("id") or row.get("marketId") or row.get("conditionId") or "")
        if fallback_event is not None:
            for market in fallback_event.markets:
                if " ".join(market.outcome_name.strip().lower().split()) == normalized_target:
                    return market.market_id
        return None

    def _parse_market(self, event_id: str, row: dict[str, Any]) -> OutcomeMarket | None:
        market_id = str(row.get("id") or row.get("marketId") or row.get("conditionId") or "")
        if not market_id:
            return None

        tokens = _load_list(row.get("clobTokenIds") or row.get("tokenIds") or row.get("clob_token_ids"))
        if not tokens and isinstance(row.get("tokens"), list):
            tokens = [
                str(token.get("token_id") or token.get("id") or token.get("tokenId") or "")
                for token in row["tokens"]
            ]
        yes_token = str(tokens[0]) if len(tokens) >= 1 else str(row.get("yesTokenId") or "")
        no_token = str(tokens[1]) if len(tokens) >= 2 else str(row.get("noTokenId") or "")
        if not yes_token or not no_token:
            return None

        question = str(row.get("question") or row.get("title") or row.get("market") or market_id)
        outcomes = _load_list(row.get("outcomes") or row.get("groupItemTitles"))
        outcome_name = str(row.get("outcome") or row.get("groupItemTitle") or (outcomes[0] if outcomes else question))

        yes_price = _as_float(
            row.get("yesPrice")
            or row.get("price")
            or row.get("lastTradePrice")
            or row.get("bestAsk"),
            default=0.0,
        )
        no_price = _as_float(row.get("noPrice"), default=max(0.0, 1.0 - yes_price) if yes_price else 0.0)
        tick_size = _as_float(row.get("minimumTickSize") or row.get("tickSize"), default=0.01)

        return OutcomeMarket(
            event_id=event_id,
            market_id=market_id,
            question=question,
            outcome_name=outcome_name,
            yes_token_id=yes_token,
            no_token_id=no_token,
            current_yes_price=yes_price,
            current_no_price=no_price,
            liquidity=_as_float(row.get("liquidity")),
            tick_size=tick_size or 0.01,
            fees_enabled=_as_bool(row.get("feesEnabled") or row.get("enableOrderBook")),
            status=str(row.get("status") or "active").lower(),
            raw=row,
        )
