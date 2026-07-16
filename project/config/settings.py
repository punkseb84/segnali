"""Centralized configuration loader for the modular quant platform."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


DEFAULT_COLLECTOR_PAIRS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD", "DOGE/USD", "LINK/USD", "AVAX/USD",
    "AAVE/USD", "LTC/USD", "BCH/USD", "TAO/USD", "DOT/USD", "XLM/USD", "TRX/USD", "ATOM/USD",
    "ETC/USD", "FIL/USD", "NEAR/USD", "UNI/USD",
]
DEFAULT_COLLECTOR_TIMEFRAMES = ["5m", "15m", "1h", "4h"]


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _notification_engine_enabled() -> bool:
    """Prefer the modular flag, but accept the legacy Railway live-signal flag."""
    if os.getenv("ENABLE_NOTIFICATION_ENGINE") is not None:
        return _bool_env("ENABLE_NOTIFICATION_ENGINE", False)
    if os.getenv("ENABLE_LIVE_SIGNALS") is not None:
        return _bool_env("ENABLE_LIVE_SIGNALS", False)
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        return True
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


def _signal_classes_env(name: str, default: list[str]) -> list[str]:
    allowed = {"A", "B", "C"}
    configured = [item for item in _list_env(name, default) if item in allowed]
    return configured or default


def _float_map_env(name: str) -> dict[str, float]:
    """Parse either JSON or PAIR=rate comma-separated mappings."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {
                str(key).strip().upper(): max(0.0, float(value))
                for key, value in parsed.items()
            }
    except (TypeError, ValueError, json.JSONDecodeError):
        pass

    result: dict[str, float] = {}
    for item in raw.split(","):
        if "=" not in item:
            continue
        pair, value = item.split("=", maxsplit=1)
        try:
            result[pair.strip().upper()] = max(0.0, float(value.strip()))
        except ValueError:
            continue
    return result


@dataclass(frozen=True)
class PlatformSettings:
    run_mode: str = os.getenv("RUN_MODE", "RAILWAY_LIGHT").strip().upper()
    database_url: str = os.getenv("DATABASE_URL", "")
    sqlite_cache_enabled: bool = _bool_env("SQLITE_CACHE_ENABLED", False)
    enable_bootstrap_test: bool = _bool_env("ENABLE_BOOTSTRAP_TEST", False)
    exchange_name: str = os.getenv("EXCHANGE_NAME", "Kraken")
    kraken_api_base: str = os.getenv("KRAKEN_API_BASE", "https://api.kraken.com/0/public")
    kraken_api_sleep_seconds: float = _float_env("KRAKEN_API_SLEEP_SECONDS", 1.2)
    kraken_max_retries: int = _int_env("KRAKEN_MAX_RETRIES", 3)
    kraken_timeout_seconds: int = _int_env("KRAKEN_TIMEOUT_SECONDS", 20)
    enable_data_collector: bool = _bool_env("ENABLE_DATA_COLLECTOR", True)
    enable_research_engine: bool = _bool_env("ENABLE_RESEARCH_ENGINE", True)
    enable_decision_engine: bool = _bool_env("ENABLE_DECISION_ENGINE", True)
    enable_strategy_engine: bool = _bool_env("ENABLE_STRATEGY_ENGINE", True)
    enable_position_monitor: bool = _bool_env("ENABLE_POSITION_MONITOR", True)
    enable_notification_engine: bool = field(default_factory=_notification_engine_enabled)
    collector_pairs: list[str] = field(default_factory=lambda: _list_env("COLLECTOR_PAIRS", DEFAULT_COLLECTOR_PAIRS))
    collector_timeframes: list[str] = field(default_factory=lambda: [item.strip().lower() for item in os.getenv("COLLECTOR_TIMEFRAMES", ",".join(DEFAULT_COLLECTOR_TIMEFRAMES)).split(",") if item.strip()])
    operational_timeframe: str = os.getenv("OPERATIONAL_TIMEFRAME", "15m").strip().lower()
    min_signal_profit_factor: float = _float_env("MIN_SIGNAL_PROFIT_FACTOR", 1.05)
    trade_notional_eur: float = _float_env("TRADE_NOTIONAL_EUR", 100.0)
    binance_buy_fee_rate: float = _float_env("BINANCE_BUY_FEE_RATE", 0.001)
    binance_sell_fee_rate: float = _float_env("BINANCE_SELL_FEE_RATE", 0.001)
    binance_spread_rate: float = _float_env("BINANCE_SPREAD_RATE", 0.0005)
    binance_pair_spread_rates: dict[str, float] = field(default_factory=lambda: _float_map_env("BINANCE_PAIR_SPREAD_RATES"))
    binance_slippage_rate: float = _float_env("BINANCE_SLIPPAGE_RATE", 0.0)
    binance_quantity_step: float = _float_env("BINANCE_QUANTITY_STEP", 0.000001)
    binance_min_qty: float = _float_env("BINANCE_MIN_QTY", 0.0)
    binance_min_notional_eur: float = _float_env("BINANCE_MIN_NOTIONAL_EUR", 10.0)
    min_tp1_net_profit_eur: float = _float_env("MIN_TP1_NET_PROFIT_EUR", 0.30)
    min_net_rr: float = _float_env("MIN_NET_RR", 1.05)
    min_historical_ev_eur: float = _float_env("MIN_HISTORICAL_EV_EUR", 0.0)
    historical_ev_tolerance_eur: float = _float_env("HISTORICAL_EV_TOLERANCE_EUR", 0.05)
    hard_block_negative_historical_ev: bool = _bool_env("HARD_BLOCK_NEGATIVE_HISTORICAL_EV", False)
    min_live_setup_score: float = _float_env("MIN_LIVE_SETUP_SCORE", 45.0)
    min_operative_signal_score: float = _float_env("MIN_OPERATIVE_SIGNAL_SCORE", 55.0)
    min_watchlist_signal_score: float = _float_env("MIN_WATCHLIST_SIGNAL_SCORE", 30.0)
    # MIN_RESEARCH_TRADES is now the class-A confidence threshold, not a hard candidate filter.
    min_research_trades: int = _int_env("MIN_RESEARCH_TRADES", 40)
    min_research_trades_b: int = _int_env("MIN_RESEARCH_TRADES_B", 20)
    require_regime_alignment_for_operative: bool = _bool_env("REQUIRE_REGIME_ALIGNMENT_FOR_OPERATIVE", False)
    allow_high_score_regime_mismatch_b: bool = _bool_env("ALLOW_HIGH_SCORE_REGIME_MISMATCH_B", True)
    dynamic_economic_target: bool = _bool_env("DYNAMIC_ECONOMIC_TARGET", True)
    min_watchlist_notification_net_profit_eur: float = _float_env("MIN_WATCHLIST_NOTIFICATION_NET_PROFIT_EUR", 0.15)
    min_watchlist_notification_net_rr: float = _float_env("MIN_WATCHLIST_NOTIFICATION_NET_RR", 0.65)
    min_watchlist_notification_live_score: float = _float_env("MIN_WATCHLIST_NOTIFICATION_LIVE_SCORE", 40.0)
    min_watchlist_progress_ratio: float = _float_env("MIN_WATCHLIST_PROGRESS_RATIO", 0.50)
    min_watchlist_sample_size: int = _int_env("MIN_WATCHLIST_SAMPLE_SIZE", 10)
    max_take_profit_distance_pct: float = _float_env("MAX_TAKE_PROFIT_DISTANCE_PCT", 0.03)
    min_stop_atr_ratio: float = _float_env("MIN_STOP_ATR_RATIO", 0.75)
    min_stop_spread_multiple: float = _float_env("MIN_STOP_SPREAD_MULTIPLE", 2.0)
    max_tp_atr_multiple: float = _float_env("MAX_TP_ATR_MULTIPLE", 8.0)
    historical_mfe_percentile: float = _float_env("HISTORICAL_MFE_PERCENTILE", 90.0)
    min_probability_sample_size: int = _int_env("MIN_PROBABILITY_SAMPLE_SIZE", 30)
    probability_horizon_candles: int = _int_env("PROBABILITY_HORIZON_CANDLES", 8)
    signal_classes: list[str] = field(default_factory=lambda: _signal_classes_env("SIGNAL_CLASSES", ["A", "B"]))
    operative_signal_classes: list[str] = field(default_factory=lambda: _signal_classes_env("OPERATIVE_SIGNAL_CLASSES", ["A", "B"]))
    watchlist_signal_classes: list[str] = field(default_factory=lambda: _signal_classes_env("WATCHLIST_SIGNAL_CLASSES", ["C"]))
    enable_watchlist_alerts: bool = _bool_env("ENABLE_WATCHLIST_ALERTS", True)
    strategy_ohlc_limit: int = _int_env("STRATEGY_OHLC_LIMIT", 720)
    strategy_candidate_limit: int = field(default_factory=lambda: _int_env("STRATEGY_CANDIDATE_LIMIT", 100))
    ambiguous_candle_mode: str = os.getenv("AMBIGUOUS_CANDLE_MODE", "conservative").strip().lower()
    enable_daily_signal_report: bool = _bool_env("ENABLE_DAILY_SIGNAL_REPORT", True)
    daily_signal_report_hours: int = _int_env("DAILY_SIGNAL_REPORT_HOURS", 24)
    daily_report_time: str = os.getenv("DAILY_REPORT_TIME", "09:00").strip()
    daily_report_timezone: str = os.getenv("DAILY_REPORT_TIMEZONE", "Europe/Rome").strip()
    dry_run: bool = _bool_env("DRY_RUN", True)
    diagnostic_mode: bool = _bool_env("DIAGNOSTIC_MODE", True)
    telegram_send_startup_message: bool = _bool_env("TELEGRAM_SEND_STARTUP_MESSAGE", True)
    telegram_test_on_startup: bool = _bool_env("TELEGRAM_TEST_ON_STARTUP", False)
    telegram_max_message_length: int = _int_env("TELEGRAM_MAX_MESSAGE_LENGTH", 3900)
    telegram_max_retries: int = _int_env("TELEGRAM_MAX_RETRIES", 3)
    signal_cooldown_minutes: int = _int_env("SIGNAL_COOLDOWN_MINUTES", 45)
    ohlc_limit: int = _int_env("COLLECTOR_OHLC_LIMIT", 720)
    scheduler_collector_seconds: int = _int_env("SCHEDULER_COLLECTOR_SECONDS", 300)
    run_data_collector_on_startup: bool = _bool_env("RUN_DATA_COLLECTOR_ON_STARTUP", True)
    run_research_on_startup: bool = _bool_env("RUN_RESEARCH_ON_STARTUP", False)
    scheduler_decision_seconds: int = _int_env("SCHEDULER_DECISION_SECONDS", 300)
    scheduler_strategy_seconds: int = _int_env("SCHEDULER_STRATEGY_SECONDS", 60)
    scheduler_position_monitor_seconds: int = _int_env("SCHEDULER_POSITION_MONITOR_SECONDS", 60)
    research_batch_size: int = _int_env("RESEARCH_BATCH_SIZE", 100)
    max_research_runtime_minutes: int = _int_env("MAX_RESEARCH_RUNTIME_MINUTES", 10)
    research_sleep_between_batches_seconds: int = _int_env("RESEARCH_SLEEP_BETWEEN_BATCHES_SECONDS", 60)
    resume_research: bool = _bool_env("RESUME_RESEARCH", True)
    scheduler_research_seconds: int = _int_env("SCHEDULER_RESEARCH_SECONDS", 86400)
    railway_light_research_seconds: int = _int_env("RAILWAY_LIGHT_RESEARCH_SECONDS", 3600)
    max_runtime_minutes: int = _int_env("MAX_MODULE_RUNTIME_MINUTES", 45)


def load_settings() -> PlatformSettings:
    return PlatformSettings()
