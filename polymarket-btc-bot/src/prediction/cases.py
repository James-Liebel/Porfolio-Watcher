from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .io import iter_jsonl, parse_timestamp


@dataclass(frozen=True)
class EventCase:
    event_id: str
    title: str
    cutoff: datetime
    resolved_yes: bool
    """Market-implied probability of YES at the decision cutoff (0–1), for baseline comparison."""
    market_yes_price: float
    news_before: tuple[dict[str, Any], ...]
    history_before: tuple[dict[str, Any], ...]


def _index_by_event(rows: list[dict[str, Any]], key: str = "event_id") -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        eid = str(row.get(key, "")).strip()
        if not eid:
            continue
        out.setdefault(eid, []).append(row)
    return out


def build_event_cases(
    events_path: Path,
    news_path: Path | None,
    history_path: Path | None,
) -> list[EventCase]:
    events = list(iter_jsonl(events_path))
    news_rows = list(iter_jsonl(news_path)) if news_path and news_path.is_file() else []
    hist_rows = list(iter_jsonl(history_path)) if history_path and history_path.is_file() else []

    news_by = _index_by_event(news_rows)
    hist_by = _index_by_event(hist_rows)

    cases: list[EventCase] = []
    for row in events:
        eid = str(row["event_id"]).strip()
        cutoff = parse_timestamp(str(row["cutoff_time"]))
        title = str(row.get("title", eid))

        n_before: list[dict[str, Any]] = []
        for nr in news_by.get(eid, []):
            t = parse_timestamp(str(nr["time"]))
            if t < cutoff:
                n_before.append(nr)

        h_before: list[dict[str, Any]] = []
        for hr in hist_by.get(eid, []):
            t = parse_timestamp(str(hr["time"]))
            if t < cutoff:
                h_before.append(hr)

        n_before.sort(key=lambda r: parse_timestamp(str(r["time"])))
        h_before.sort(key=lambda r: parse_timestamp(str(r["time"])))

        cases.append(
            EventCase(
                event_id=eid,
                title=title,
                cutoff=cutoff,
                resolved_yes=bool(row["resolved_yes"]),
                market_yes_price=float(row["market_yes_price"]),
                news_before=tuple(n_before),
                history_before=tuple(h_before),
            )
        )

    cases.sort(key=lambda c: c.event_id)
    return cases
