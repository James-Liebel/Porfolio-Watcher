"""pytest config: add polymarket-btc-bot root to sys.path so `src` is importable."""
import sys
import os

import pytest

# Make `src` importable from tests/
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def anyio_backend():
    return "asyncio"
