from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Settings
from ..storage.db import Database
from .engine import ArbEngine
from .models import ArbEvent, OutcomeMarket, PriceLevel, TokenBook
from .repository import ArbRepository


def decode_event(raw: dict[str, Any]) -> ArbEvent:
    markets = [
        OutcomeMarket(
            event_id=raw["event_id"],
            market_id=market["market_id"],
            question=market["question"],
            outcome_name=market["outcome_name"],
            yes_token_id=market["yes_token_id"],
            no_token_id=market["no_token_id"],
            current_yes_price=float(market.get("current_yes_price", 0.0)),
            current_no_price=float(market.get("current_no_price", 0.0)),
            liquidity=float(market.get("liquidity", 0.0)),
            tick_size=float(market.get("tick_size", 0.01)),
            fees_enabled=bool(market.get("fees_enabled", False)),
            status=str(market.get("status", "active")),
        )
        for market in raw.get("markets", [])
    ]
    return ArbEvent(
        event_id=raw["event_id"],
        title=raw["title"],
        category=str(raw.get("category", "")),
        neg_risk=bool(raw.get("neg_risk", False)),
        enable_neg_risk=bool(raw.get("enable_neg_risk", False)),
        neg_risk_augmented=bool(raw.get("neg_risk_augmented", False)),
        status=str(raw.get("status", "active")),
        liquidity=float(raw.get("liquidity", 0.0)),
        rules_text=str(raw.get("rules_text", "")),
        end_time=str(raw.get("end_time", "")),
        markets=markets,
        raw=dict(raw),
    )


def decode_book(token_id: str, raw: dict[str, Any]) -> TokenBook:
    bids = [PriceLevel(price=float(level["price"]), size=float(level["size"])) for level in raw.get("bids", [])]
    asks = [PriceLevel(price=float(level["price"]), size=float(level["size"])) for level in raw.get("asks", [])]
    return TokenBook(
        token_id=token_id,
        timestamp=datetime.fromisoformat(raw["timestamp"]) if raw.get("timestamp") else datetime.now(timezone.utc),
        best_bid=float(raw.get("best_bid", 0.0)),
        best_ask=float(raw.get("best_ask", 0.0)),
        bids=bids,
        asks=asks,
        fees_enabled=bool(raw.get("fees_enabled", False)),
        tick_size=float(raw.get("tick_size", 0.01)),
        source=str(raw.get("source", "replay")),
    )


def load_cycle_records(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            payload = json.loads(line)
            if payload.get("record_type") == "cycle":
                records.append(payload)
    return records


def _round_number(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    return value


def _canonical_leg(leg: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_id": leg["market_id"],
        "token_id": leg["token_id"],
        "outcome_name": leg["outcome_name"],
        "position_side": leg["position_side"],
        "action": leg["action"],
        "price": round(float(leg["price"]), 6),
        "size": round(float(leg["size"]), 6),
        "fees_enabled": bool(leg.get("fees_enabled", False)),
    }


def _canonical_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event["event_id"],
        "title": event["title"],
        "status": str(event.get("status", "")),
        "liquidity": round(float(event.get("liquidity", 0.0)), 6),
        "markets": sorted(
            [
                {
                    "market_id": market["market_id"],
                    "outcome_name": market["outcome_name"],
                    "yes_token_id": market["yes_token_id"],
                    "no_token_id": market["no_token_id"],
                }
                for market in event.get("markets", [])
            ],
            key=lambda item: item["market_id"],
        ),
    }


def canonicalize_cycle_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    summary = snapshot.get("summary", {})
    last_cycle = summary.get("last_cycle", snapshot.get("summary", {}).get("last_cycle", {})) or snapshot.get("last_cycle", {})
    canonical = {
        "summary": {
            "available_cash": round(float(summary.get("available_cash", 0.0)), 6),
            "cash": round(float(summary.get("cash", 0.0)), 6),
            "equity": round(float(summary.get("equity", 0.0)), 6),
            "contributed_capital": round(float(summary.get("contributed_capital", 0.0)), 6),
            "realized_pnl": round(float(summary.get("realized_pnl", 0.0)), 6),
            "fees_paid": round(float(summary.get("fees_paid", 0.0)), 6),
            "rebates_earned": round(float(summary.get("rebates_earned", 0.0)), 6),
            "open_positions": int(summary.get("open_positions", 0)),
            "open_orders": int(summary.get("open_orders", 0)),
            "open_baskets": int(summary.get("open_baskets", 0)),
            "executed_count": int(summary.get("executed_count", 0)),
            "rejected_count": int(summary.get("rejected_count", 0)),
            "tracked_events": int(summary.get("tracked_events", 0)),
            "latest_opportunities": int(summary.get("latest_opportunities", 0)),
            "trading_halted": bool(summary.get("trading_halted", False)),
            "halt_reason": str(summary.get("halt_reason", "")),
            "last_cycle": {
                "tracked_events": int(last_cycle.get("tracked_events", 0)),
                "tracked_books": int(last_cycle.get("tracked_books", 0)),
                "auto_settled": int(last_cycle.get("auto_settled", 0)),
                "opportunities": int(last_cycle.get("opportunities", 0)),
                "executed": int(last_cycle.get("executed", 0)),
            },
        },
        "positions": sorted(
            [
                {
                    "token_id": position["token_id"],
                    "market_id": position["market_id"],
                    "event_id": position["event_id"],
                    "contract_side": position["contract_side"],
                    "size": round(float(position["size"]), 6),
                    "avg_price": round(float(position["avg_price"]), 6),
                }
                for position in snapshot.get("positions", [])
            ],
            key=lambda item: (item["event_id"], item["market_id"], item["token_id"]),
        ),
        "orders": sorted(
            [
                {
                    "market_id": order["market_id"],
                    "token_id": order["token_id"],
                    "side": order["side"],
                    "price": round(float(order["price"]), 6),
                    "size": round(float(order["size"]), 6),
                    "order_type": order["order_type"],
                    "maker_or_taker": order["maker_or_taker"],
                    "status": order["status"],
                    "filled_size": round(float(order.get("filled_size", 0.0)), 6),
                    "average_price": round(float(order.get("average_price", 0.0)), 6),
                }
                for order in snapshot.get("orders", [])
            ],
            key=lambda item: (
                item["market_id"],
                item["token_id"],
                item["side"],
                item["price"],
                item["size"],
                item["status"],
            ),
        ),
        "baskets": sorted(
            [
                {
                    "event_id": basket["event_id"],
                    "strategy_type": basket["strategy_type"],
                    "status": basket["status"],
                    "capital_reserved": round(float(basket["capital_reserved"]), 6),
                    "target_net_edge_bps": round(float(basket["target_net_edge_bps"]), 6),
                    "realized_net_pnl": round(float(basket.get("realized_net_pnl", 0.0)), 6),
                }
                for basket in snapshot.get("baskets", [])
            ],
            key=lambda item: (
                item["event_id"],
                item["strategy_type"],
                item["capital_reserved"],
                item["status"],
            ),
        ),
        "opportunities": sorted(
            [
                {
                    "strategy_type": opportunity["strategy_type"],
                    "event_id": opportunity["event_id"],
                    "gross_edge_bps": round(float(opportunity["gross_edge_bps"]), 6),
                    "net_edge_bps": round(float(opportunity["net_edge_bps"]), 6),
                    "capital_required": round(float(opportunity["capital_required"]), 6),
                    "expected_profit": round(float(opportunity["expected_profit"]), 6),
                    "requires_conversion": bool(opportunity.get("requires_conversion", False)),
                    "settle_on_resolution": bool(opportunity.get("settle_on_resolution", False)),
                    "legs": sorted(
                        [_canonical_leg(leg) for leg in opportunity.get("legs", [])],
                        key=lambda leg: (leg["market_id"], leg["token_id"], leg["action"], leg["price"]),
                    ),
                }
                for opportunity in snapshot.get("opportunities", [])
            ],
            key=lambda item: (
                item["strategy_type"],
                item["event_id"],
                item["capital_required"],
                item["net_edge_bps"],
            ),
        ),
        "auto_settlements": sorted(
            [
                {
                    "event_id": entry["event_id"],
                    "resolution_market_id": entry["resolution_market_id"],
                    "pnl_realized": round(float(entry.get("pnl_realized", 0.0)), 6),
                    "resolution_source": str(entry.get("resolution_source", "")),
                    "resolved_event": _canonical_event(entry["resolved_event"]) if entry.get("resolved_event") else None,
                }
                for entry in snapshot.get("auto_settlements", [])
            ],
            key=lambda item: (item["event_id"], item["resolution_market_id"]),
        ),
    }
    return canonical


class ReplayUniverseService:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records
        self._index = 0

    @property
    def current_record(self) -> dict[str, Any]:
        if self._index >= len(self._records):
            raise IndexError("replay cursor is past the end of the session")
        return self._records[self._index]

    async def refresh(self) -> list[ArbEvent]:
        raw_events = self.current_record.get("active_events")
        if raw_events is None:
            raw_events = self.current_record.get("events", [])
        return [decode_event(event) for event in raw_events]

    async def lookup_resolution(
        self,
        event_id: str,
        fallback_event: ArbEvent | None = None,
    ) -> tuple[ArbEvent | None, str | None, str]:
        for entry in self.current_record.get("auto_settlements", []):
            if entry.get("event_id") != event_id:
                continue
            resolved_event = entry.get("resolved_event")
            return (
                decode_event(resolved_event) if isinstance(resolved_event, dict) else fallback_event,
                entry.get("resolution_market_id"),
                str(entry.get("resolution_source", "replay")),
            )
        return fallback_event, None, "unresolved"

    async def close(self) -> None:
        return None

    def advance(self) -> None:
        self._index += 1


class ReplayMarketDataService:
    def __init__(self, universe: ReplayUniverseService) -> None:
        self._universe = universe

    async def refresh(self, events: list[ArbEvent]) -> dict[str, TokenBook]:
        del events
        raw_books = self._universe.current_record.get("books", {})
        return {
            token_id: decode_book(token_id, raw)
            for token_id, raw in raw_books.items()
        }


async def replay_cycle_records(
    records: list[dict[str, Any]],
    config: Settings,
) -> dict[str, Any]:
    if not records:
        return {"cycles": [], "mismatch_count": 0}

    fd, path = tempfile.mkstemp(suffix="-arb-replay.db")
    os.close(fd)
    universe = ReplayUniverseService(records)
    market_data = ReplayMarketDataService(universe)
    legacy_db = Database(path=path)
    repository = ArbRepository(path=path)
    engine = ArbEngine(
        config=config,
        legacy_db=legacy_db,
        repository=repository,
        universe=universe,
        market_data=market_data,
    )
    results: list[dict[str, Any]] = []
    try:
        await engine.initialize()
        for record in records:
            await engine.run_cycle()
            replayed_snapshot = engine.cycle_snapshot()
            replayed_snapshot["summary"]["last_cycle"] = dict(engine.summary().get("last_cycle", {}))
            recorded_canonical = canonicalize_cycle_snapshot(record)
            replayed_canonical = canonicalize_cycle_snapshot(replayed_snapshot)
            mismatch_fields = sorted(
                key
                for key in recorded_canonical.keys()
                if recorded_canonical[key] != replayed_canonical[key]
            )
            results.append(
                {
                    "cycle_index": int(record.get("cycle_index", len(results) + 1)),
                    "matched": not mismatch_fields,
                    "mismatch_fields": mismatch_fields,
                    "recorded": recorded_canonical,
                    "replayed": replayed_canonical,
                }
            )
            universe.advance()
    finally:
        await engine.shutdown()
        try:
            os.remove(path)
        except OSError:
            pass
    return {
        "cycles": results,
        "mismatch_count": sum(1 for result in results if not result["matched"]),
    }
