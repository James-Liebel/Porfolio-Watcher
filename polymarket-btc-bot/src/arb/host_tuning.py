"""
Host-aware defaults for the single structural-arb runtime (one bot, full bankroll).

Scales parallel CLOB work, Gamma pagination, and tracked-event breadth with CPU count.
I/O-bound fetches use higher concurrency on many-core machines; caps avoid API abuse.
"""
from __future__ import annotations

import os
from typing import Literal

Mode = Literal["live", "paper"]


def cpu_count_safe() -> int:
    n = os.cpu_count()
    return max(2, int(n or 4))


def structural_bot_env_from_cpu(mode: Mode = "live") -> dict[str, str]:
    """
    Env string overrides to merge after static launcher dict / .env.

    Paper mode uses ~72% of live concurrency (more Python-side simulation per cycle).
    """
    n = cpu_count_safe()
    scale = 0.72 if mode == "paper" else 1.0

    # Parallel get_order_book HTTP calls per cycle (I/O bound).
    conc = int(min(72, max(14, round(n * 4.0 * scale))))
    # Events that receive CLOB books each cycle.
    # Keep this moderate so live cycles complete consistently on commodity hosts.
    tracked = int(min(850, max(220, round(210 + n * 38 * scale))))
    # Execution slots (still bounded by cash / risk).
    max_opp = int(min(18, max(7, n // 2 + 6)))
    max_open = int(min(14, max(6, n // 2 + 4)))
    per_strat = int(min(8, max(4, n // 3 + 3)))
    # Shorter poll for faster reaction once a cycle completes.
    poll = int(max(5, min(11, 13 - n // 4)))

    # Gamma: more pages when CPU can handle the merge/sort work (still one asyncio client).
    ev_pages = int(min(45, max(24, round(20 + n * 1.8))))
    mkt_pages = int(min(65, max(38, round(36 + n * 2.0))))
    if mode == "paper":
        ev_pages = max(22, int(ev_pages * 0.88))
        mkt_pages = max(34, int(mkt_pages * 0.88))
    gamma_timeout = 75.0 if mode == "live" else 68.0

    return {
        "CLOB_BOOK_FETCH_CONCURRENCY": str(conc),
        "MAX_TRACKED_EVENTS": str(tracked),
        "MAX_OPPORTUNITIES_PER_CYCLE": str(max_opp),
        "MAX_TOTAL_OPEN_BASKETS": str(max_open),
        "MAX_BASKETS_PER_STRATEGY": str(per_strat),
        "ARB_POLL_SECONDS": str(poll),
        "GAMMA_EVENT_MAX_PAGES": str(ev_pages),
        "GAMMA_MARKET_MAX_PAGES": str(mkt_pages),
        "GAMMA_HTTP_TIMEOUT_SECONDS": f"{gamma_timeout:.1f}",
    }
