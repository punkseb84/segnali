"""Runtime configuration for the crypto signal worker."""

from __future__ import annotations

import os
from pathlib import Path


def get_float_env(name: str, default: float) -> float:
    """Read a float environment variable with a safe fallback."""
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


INITIAL_CAPITAL = get_float_env("INITIAL_CAPITAL", 100.0)
BUY_FEE_PERCENT = get_float_env("BUY_FEE_PERCENT", 0.10)
SELL_FEE_PERCENT = get_float_env("SELL_FEE_PERCENT", 0.10)
SPREAD_PERCENT = get_float_env("SPREAD_PERCENT", 0.0)
SLIPPAGE_PERCENT = get_float_env("SLIPPAGE_PERCENT", 0.0)
MIN_NET_RR = get_float_env("MIN_NET_RR", 1.30)
SAME_CANDLE_PRIORITY = os.getenv("SAME_CANDLE_PRIORITY", "SL").strip().upper()
EQUITY_STATE_FILE = Path(os.getenv("EQUITY_STATE_FILE", "equity_state.json"))
