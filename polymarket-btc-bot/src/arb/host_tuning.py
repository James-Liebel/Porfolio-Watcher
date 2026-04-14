"""
Host-aware defaults for the single structural-arb runtime.

Scales parallel CLOB work and universe breadth with CPU count. I/O-bound fetches use
higher concurrency on many-core machines; caps avoid hammering Polymarket APIs.
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

    Paper mode uses ~70% of live concurrency (more Python-side simulation per cycle).
    """
    n = cpu_count_safe()
    scale = 0.72 if mode == "paper" else 1.0

    # Parallel get_order_book HTTP calls per cycle (I/O bound).
    conc = int(min(56, max(12, round(n * 3.5 * scale))))
    # Events that receive CLOB books each cycle (breadth vs latency).
    tracked = int(min(900, max(380, round(320 + n * 52 * scale))))
    # Execution slots (still bounded by cash / risk).
    max_opp = int(min(14, max(6, n // 2 + 5)))
    max_open = int(min(10, max(5, n // 3 + 4)))
    per_strat = int(min(6, max(3, n // 4 + 2)))
    # Slightly shorter poll on fast machines; floor keeps API load reasonable.
    poll = int(max(10, min(18, 22 - n // 3)))

    return {
        "CLOB_BOOK_FETCH_CONCURRENCY": str(conc),
        "MAX_TRACKED_EVENTS": str(tracked),
        "MAX_OPPORTUNITIES_PER_CYCLE": str(max_opp),
        "MAX_TOTAL_OPEN_BASKETS": str(max_open),
        "MAX_BASKETS_PER_STRATEGY": str(per_strat),
        "ARB_POLL_SECONDS": str(poll),
    }
