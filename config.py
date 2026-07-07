"""Configuration for the Railway crypto signal worker."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

load_dotenv()


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default



def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}

def _list_env(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return default
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


TELEGRAM_BOT_TOKEN: Final[str] = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID: Final[str] = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SUPPORTED_MODES: Final[tuple[str, ...]] = ("CONSERVATIVE", "SCALPING_FAST")
MODE: Final[str] = os.getenv("MODE", "SCALPING_FAST").strip().upper()
ACTIVE_MODE: Final[str] = MODE if MODE in SUPPORTED_MODES else "SCALPING_FAST"

TRADE_AMOUNT_EUR: Final[float] = _float_env("TRADE_AMOUNT_EUR", 10.0)
FEE_BUY_PERCENT: Final[float] = _float_env("FEE_BUY_PERCENT", 0.10)
FEE_SELL_PERCENT: Final[float] = _float_env("FEE_SELL_PERCENT", 0.10)
SLIPPAGE_PERCENT: Final[float] = _float_env("SLIPPAGE_PERCENT", 0.00)
SPREAD_PERCENT: Final[float] = _float_env("SPREAD_PERCENT", 0.00)
MIN_NET_RR: Final[float] = _float_env("MIN_NET_RR", 1.05)
ENABLE_WATCHLIST_ALERTS: Final[bool] = _bool_env("ENABLE_WATCHLIST_ALERTS", False)

MIN_SIGNAL_SCORE_CONSERVATIVE: Final[int] = _int_env("MIN_SIGNAL_SCORE_CONSERVATIVE", 85)
MIN_WATCHLIST_SCORE_CONSERVATIVE: Final[int] = _int_env("MIN_WATCHLIST_SCORE_CONSERVATIVE", 70)
MIN_SIGNAL_SCORE_SCALPING: Final[int] = _int_env("MIN_SIGNAL_SCORE_SCALPING", 68)
MIN_WATCHLIST_SCORE_SCALPING: Final[int] = _int_env("MIN_WATCHLIST_SCORE_SCALPING", 60)

DAILY_REPORT_HOUR: Final[int] = _int_env("DAILY_REPORT_HOUR", 9)
LOOP_SLEEP_SECONDS: Final[int] = _int_env("LOOP_SLEEP_SECONDS", 300)
OHLC_LIMIT: Final[int] = _int_env("OHLC_LIMIT", 300)
DATABASE_PATH: Final[Path] = Path(os.getenv("DATABASE_PATH", "signals.db"))

PAIRS: Final[list[str]] = _list_env(
    "PAIRS",
    [
        "BTC/USD",
        "ETH/USD",
        "SOL/USD",
        "LINK/USD",
        "UNI/USD",
        "AAVE/USD",
        "LTC/USD",
        "BCH/USD",
        "AVAX/USD",
        "TAO/USD",
        "XRP/USD",
        "ADA/USD",
        "DOGE/USD",
    ],
)

TIMEFRAMES: Final[dict[str, dict[str, str]]] = {
    "SCALPING_FAST": {"main": "5m", "confirm_1": "15m", "confirm_2": "1h"},
    "CONSERVATIVE": {"main": "15m", "confirm_1": "1h", "confirm_2": "4h"},
}

MAX_SIGNALS_PER_DAY: Final[int] = _int_env("MAX_SIGNALS_PER_DAY", 30)
MAX_SIGNALS_PER_PAIR_PER_DAY: Final[int] = _int_env("MAX_SIGNALS_PER_PAIR_PER_DAY", 3)
DUPLICATE_MINUTES: Final[int] = _int_env("DUPLICATE_MINUTES", 60)

KRAKEN_API_BASE: Final[str] = "https://api.kraken.com/0/public"
EXCHANGE_NAME: Final[str] = "Kraken"
DIRECTION: Final[str] = "LONG"
