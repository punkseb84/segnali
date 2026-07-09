"""Centralized configuration loader for the modular quant platform."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


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


def _list_env(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return default
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class PlatformSettings:
    database_url: str = os.getenv("DATABASE_URL", "")
    exchange_name: str = os.getenv("EXCHANGE_NAME", "Kraken")
    kraken_api_base: str = os.getenv("KRAKEN_API_BASE", "https://api.kraken.com/0/public")
    kraken_api_sleep_seconds: float = _float_env("KRAKEN_API_SLEEP_SECONDS", 1.2)
    kraken_max_retries: int = _int_env("KRAKEN_MAX_RETRIES", 3)
    kraken_timeout_seconds: int = _int_env("KRAKEN_TIMEOUT_SECONDS", 20)
    enable_data_collector: bool = _bool_env("ENABLE_DATA_COLLECTOR", True)
    enable_research_engine: bool = _bool_env("ENABLE_RESEARCH_ENGINE", False)
    enable_decision_engine: bool = _bool_env("ENABLE_DECISION_ENGINE", False)
    enable_strategy_engine: bool = _bool_env("ENABLE_STRATEGY_ENGINE", False)
    enable_notification_engine: bool = _bool_env("ENABLE_NOTIFICATION_ENGINE", False)
    collector_pairs: list[str] = field(default_factory=lambda: _list_env("COLLECTOR_PAIRS", ["BTC/USD", "ETH/USD", "SOL/USD"]))
    collector_timeframes: list[str] = field(default_factory=lambda: [item.strip().lower() for item in os.getenv("COLLECTOR_TIMEFRAMES", "5m,15m,1h").split(",") if item.strip()])
    ohlc_limit: int = _int_env("COLLECTOR_OHLC_LIMIT", 720)
    scheduler_collector_seconds: int = _int_env("SCHEDULER_COLLECTOR_SECONDS", 300)
    scheduler_decision_seconds: int = _int_env("SCHEDULER_DECISION_SECONDS", 900)
    scheduler_strategy_seconds: int = _int_env("SCHEDULER_STRATEGY_SECONDS", 60)
    max_runtime_minutes: int = _int_env("MAX_MODULE_RUNTIME_MINUTES", 45)


def load_settings() -> PlatformSettings:
    return PlatformSettings()
