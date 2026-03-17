"""pytest config: add polymarket-btc-bot root to sys.path so `src` is importable."""
import sys
import os

# Make `src` importable from tests/
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
