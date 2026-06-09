"""Regression test: Gamma list pagination must continue when the server caps a page
below the requested limit (Gamma returns ~100 rows even if limit=500)."""
from __future__ import annotations

import pytest

from src.arb.universe import GammaUniverseService


class _FakeResp:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._rows


class _CappedSession:
    """Simulates a server that returns at most `server_cap` rows per page regardless of
    the requested `limit`, drawn from a fixed total population."""

    def __init__(self, total_rows: int, server_cap: int = 100):
        self._all = [{"id": str(i)} for i in range(total_rows)]
        self._cap = server_cap
        self.requests: list[tuple[int, int]] = []

    def get(self, url: str, params: dict):
        limit = int(params["limit"])
        offset = int(params["offset"])
        self.requests.append((offset, limit))
        page = self._all[offset : offset + min(limit, self._cap)]
        return _FakeResp(page)


class _Cfg:
    gamma_base_url = "https://gamma-api.polymarket.com"


@pytest.mark.anyio
async def test_pagination_continues_past_server_cap():
    session = _CappedSession(total_rows=250, server_cap=100)
    svc = GammaUniverseService(config=_Cfg(), session=session)

    rows = await svc._paged_gamma_list(
        "/markets",
        {"active": "true", "closed": "false"},
        page_size=500,
        max_pages=40,
    )

    # Before the fix this stopped at 100 (first page < requested limit -> break).
    assert len(rows) == 250
    # Offset must advance by rows actually returned (100), not by page_size (500).
    offsets = [offset for offset, _ in session.requests]
    assert offsets[:3] == [0, 100, 200]


@pytest.mark.anyio
async def test_pagination_respects_max_pages_bound():
    session = _CappedSession(total_rows=10_000, server_cap=100)
    svc = GammaUniverseService(config=_Cfg(), session=session)

    rows = await svc._paged_gamma_list(
        "/markets",
        {"active": "true", "closed": "false"},
        page_size=500,
        max_pages=5,
    )

    assert len(rows) == 500
    assert len(session.requests) == 5
