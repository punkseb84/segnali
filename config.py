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


def get_bool_env(name: str, default: bool) -> bool:
    """Read a boolean environment variable with a safe fallback."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


INITIAL_CAPITAL = get_float_env("INITIAL_CAPITAL", 100.0)
BUY_FEE_PERCENT = get_float_env("BUY_FEE_PERCENT", 0.10)
SELL_FEE_PERCENT = get_float_env("SELL_FEE_PERCENT", 0.10)
SPREAD_PERCENT = get_float_env("SPREAD_PERCENT", 0.0)
SLIPPAGE_PERCENT = get_float_env("SLIPPAGE_PERCENT", 0.0)
MIN_NET_RR = get_float_env("MIN_NET_RR", 1.05)
SAME_CANDLE_PRIORITY = os.getenv("SAME_CANDLE_PRIORITY", "SL").strip().upper()
NET_RR_MODE = os.getenv("NET_RR_MODE", "BLENDED").strip().upper()
EQUITY_STATE_FILE = Path(os.getenv("EQUITY_STATE_FILE", "equity_state.json"))

MIN_SIGNAL_SCORE = get_float_env("MIN_SIGNAL_SCORE", 85.0)
ENABLE_REJECTED_SIGNALS_LOG = get_bool_env("ENABLE_REJECTED_SIGNALS_LOG", True)
DAILY_REPORT_HOUR = get_float_env("DAILY_REPORT_HOUR", 9.0)
USE_BTC_TREND_FILTER = get_bool_env("USE_BTC_TREND_FILTER", True)
AMBIGUOUS_CANDLE_POLICY = os.getenv("AMBIGUOUS_CANDLE_POLICY", "STOP_FIRST").strip().upper()
MIN_DECIMALS = int(get_float_env("MIN_DECIMALS", 3))
