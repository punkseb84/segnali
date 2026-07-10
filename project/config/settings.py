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


def _notification_engine_enabled() -> bool:
    """Prefer the modular flag, but accept the legacy Railway live-signal flag.

    Some Railway deployments already expose ENABLE_LIVE_SIGNALS but do not yet
    have ENABLE_NOTIFICATION_ENGINE. The fallback keeps the modular platform
    compatible without requiring users to rename existing variables.
    """
    if os.getenv("ENABLE_NOTIFICATION_ENGINE") is not None:
        return _bool_env("ENABLE_NOTIFICATION_ENGINE", False)
    return _bool_env("ENABLE_LIVE_SIGNALS", False)


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
    run_mode: str = os.getenv("RUN_MODE", "RAILWAY_LIGHT").strip().upper()
    database_url: str = os.getenv("DATABASE_URL", "")
    sqlite_cache_enabled: bool = _bool_env("SQLITE_CACHE_ENABLED", False)
    enable_bootstrap_test: bool = _bool_env("ENABLE_BOOTSTRAP_TEST", True)
    exchange_name: str = os.getenv("EXCHANGE_NAME", "Kraken")
    kraken_api_base: str = os.getenv("KRAKEN_API_BASE", "https://api.kraken.com/0/public")
    kraken_api_sleep_seconds: float = _float_env("KRAKEN_API_SLEEP_SECONDS", 1.2)
    kraken_max_retries: int = _int_env("KRAKEN_MAX_RETRIES", 3)
    kraken_timeout_seconds: int = _int_env("KRAKEN_TIMEOUT_SECONDS", 20)
    enable_data_collector: bool = _bool_env("ENABLE_DATA_COLLECTOR", True)
    enable_research_engine: bool = _bool_env("ENABLE_RESEARCH_ENGINE", True)
    enable_decision_engine: bool = _bool_env("ENABLE_DECISION_ENGINE", True)
    enable_strategy_engine: bool = _bool_env("ENABLE_STRATEGY_ENGINE", True)
    enable_notification_engine: bool = field(default_factory=_notification_engine_enabled)
    collector_pairs: list[str] = field(default_factory=lambda: _list_env("COLLECTOR_PAIRS", ["BTC/USD", "ETH/USD", "SOL/USD"]))
    collector_timeframes: list[str] = field(default_factory=lambda: [item.strip().lower() for item in os.getenv("COLLECTOR_TIMEFRAMES", "5m,15m,1h").split(",") if item.strip()])
    ohlc_limit: int = _int_env("COLLECTOR_OHLC_LIMIT", 720)
    scheduler_collector_seconds: int = _int_env("SCHEDULER_COLLECTOR_SECONDS", 300)
    run_data_collector_on_startup: bool = _bool_env("RUN_DATA_COLLECTOR_ON_STARTUP", True)
    run_research_on_startup: bool = _bool_env("RUN_RESEARCH_ON_STARTUP", True)
    scheduler_decision_seconds: int = _int_env("SCHEDULER_DECISION_SECONDS", 900)
    scheduler_strategy_seconds: int = _int_env("SCHEDULER_STRATEGY_SECONDS", 60)
    research_batch_size: int = _int_env("RESEARCH_BATCH_SIZE", 100)
    max_research_runtime_minutes: int = _int_env("MAX_RESEARCH_RUNTIME_MINUTES", 20)
    research_sleep_between_batches_seconds: int = _int_env("RESEARCH_SLEEP_BETWEEN_BATCHES_SECONDS", 60)
    resume_research: bool = _bool_env("RESUME_RESEARCH", True)
    scheduler_research_seconds: int = _int_env("SCHEDULER_RESEARCH_SECONDS", 86400)
    railway_light_research_seconds: int = _int_env("RAILWAY_LIGHT_RESEARCH_SECONDS", 300)
    max_runtime_minutes: int = _int_env("MAX_MODULE_RUNTIME_MINUTES", 45)


def load_settings() -> PlatformSettings:
    return PlatformSettings()
