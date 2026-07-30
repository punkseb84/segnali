"""Railway runtime for the TRIX + ADX PAPER strategy."""
from __future__ import annotations

import os
from dataclasses import replace

import project.notification_engine.service as notification_service
from project.capital_protection_migration import run_capital_protection_migration
from project.composite_position_monitor import CompositePositionMonitor
from project.config.settings import load_settings
from project.database.migrations import run_migrations
from project.database.postgres import (
    parse_postgres_connection_info,
    sanitize_postgres_error,
)
from project.daily_paper_formatters import format_daily_paper_outcome_message
from project.ichimoku_scanner import RUNTIME_VERSION as ICHIMOKU_RUNTIME_VERSION
from project.notifying_ichimoku_outcome_monitor import NotifyingIchimokuOutcomeMonitor
from project.notifying_trix_adx_outcome_monitor import NotifyingTrixAdxOutcomeMonitor
from project.notification_engine.simple_formatters import format_simple_report_message
from project.reliable_notification import ReliableNotificationEngine
from project.shared.events import EventBus
from project.shared.logging import get_module_logger
from project.simple_main import apply_simple_policy, build_collector, build_postgres
from project.trix_adx_formatters import (
    format_trix_adx_outcome_message,
    format_trix_adx_signal_message,
)
from project.trix_adx_scanner import RUNTIME_VERSION, STRATEGY_NAME, TrixAdxScanner
from project.trix_adx_scheduler import HourAlignedTrixAdxScheduler


TOP_20_PAIRS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD",
    "DOGE/USD", "LINK/USD", "AVAX/USD", "AAVE/USD", "LTC/USD",
    "BCH/USD", "TAO/USD", "DOT/USD", "XLM/USD", "TRX/USD",
    "ATOM/USD", "ETC/USD", "FIL/USD", "NEAR/USD", "UNI/USD",
]


def _enabled(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _format_runtime_outcome(payload: dict[str, object]) -> str:
    strategy = str(payload.get("strategy") or "")
    if "ICHIMOKU" in strategy.upper():
        return format_daily_paper_outcome_message(payload)
    return format_trix_adx_outcome_message(payload)


def main() -> None:
    settings = apply_simple_policy(load_settings())
    settings = replace(
        settings,
        collector_pairs=list(TOP_20_PAIRS),
        collector_timeframes=["1h", "4h"],
        operational_timeframe="1h",
        scheduler_collector_seconds=3600,
    )
    logger = get_module_logger("system")

    trix_length = int(os.getenv("TRIX_LENGTH", "20"))
    trix_signal_length = int(os.getenv("TRIX_SIGNAL_LENGTH", "9"))
    adx_length = int(os.getenv("TRIX_ADX_LENGTH", "14"))
    adx_threshold = float(os.getenv("TRIX_ADX_THRESHOLD", "25"))
    volume_ratio_min = float(os.getenv("TRIX_VOLUME_RATIO_MIN", "1.0"))
    max_extension_atr = float(os.getenv("TRIX_MAX_EXTENSION_ATR", "1.0"))
    min_stop_atr = float(os.getenv("TRIX_MIN_STOP_ATR", "1.0"))
    max_stop_atr = float(os.getenv("TRIX_MAX_STOP_ATR", "1.8"))
    trailing_atr = float(os.getenv("TRIX_TRAILING_ATR", "2.5"))
    be_buffer = float(os.getenv("TRIX_BREAK_EVEN_BUFFER_RATE", "0.0002"))
    min_score = float(os.getenv("TRIX_MIN_SCORE", "70"))
    max_per_cycle = max(1, int(os.getenv("TRIX_MAX_PER_CYCLE", "1")))
    max_per_day = max(1, int(os.getenv("TRIX_MAX_PER_DAY", "2")))
    max_open_positions = max(1, int(os.getenv("TRIX_MAX_OPEN_POSITIONS", "2")))
    max_daily_stops = max(1, int(os.getenv("TRIX_MAX_DAILY_STOPS", "2")))

    logger.info("======================================")
    logger.info("PROJECT MAIN: TRIX + ADX PAPER STRATEGY")
    logger.info("======================================")
    logger.info(
        "TRIX_ADX_RUNTIME version=%s strategy=%s trigger=1h context=4h pairs=%s "
        "trix=%s signal=%s adx=%s threshold=%.1f volume_min=%.2f "
        "max_extension_atr=%.2f stop_atr=%.2f-%.2f trailing_atr=%.2f "
        "max_per_day=%s max_open=%s telegram=true paper=true",
        RUNTIME_VERSION,
        STRATEGY_NAME,
        len(settings.collector_pairs),
        trix_length,
        trix_signal_length,
        adx_length,
        adx_threshold,
        volume_ratio_min,
        max_extension_atr,
        min_stop_atr,
        max_stop_atr,
        trailing_atr,
        max_per_day,
        max_open_positions,
    )

    try:
        postgres = build_postgres(settings)
    except Exception as exc:
        logger.error(
            "PostgreSQL connection failed: %s",
            sanitize_postgres_error(str(exc), settings.database_url),
        )
        raise SystemExit(1) from exc

    logger.info("PostgreSQL connection: OK")
    logger.info("PostgreSQL %s", parse_postgres_connection_info(settings.database_url).display())
    run_migrations(postgres)
    run_capital_protection_migration(postgres)

    event_bus = EventBus()
    notification_service.format_signal_message = format_trix_adx_signal_message
    notification_service.format_report_message = format_simple_report_message
    notification_service.format_outcome_message = _format_runtime_outcome

    collector = build_collector(settings, event_bus, postgres)
    scanner = TrixAdxScanner(
        postgres,
        event_bus,
        settings.collector_pairs,
        exchange=settings.exchange_name,
        trade_notional_eur=settings.trade_notional_eur,
        buy_fee_rate=settings.binance_buy_fee_rate,
        sell_fee_rate=settings.binance_sell_fee_rate,
        spread_rate=settings.binance_spread_rate,
        slippage_rate=settings.binance_slippage_rate,
        quantity_step=settings.binance_quantity_step,
        min_qty=settings.binance_min_qty,
        min_notional_eur=settings.binance_min_notional_eur,
        signal_cooldown_minutes=max(240, settings.signal_cooldown_minutes),
        min_signal_score=min_score,
        min_net_rr=0.0,
        min_net_profit_eur=0.0,
        max_signals_per_cycle=max_per_cycle,
        enable_daily_signal_report=settings.enable_daily_signal_report,
        daily_signal_report_hours=settings.daily_signal_report_hours,
        trix_length=trix_length,
        trix_signal_length=trix_signal_length,
        adx_length=adx_length,
        adx_threshold=adx_threshold,
        volume_ratio_min=volume_ratio_min,
        max_extension_atr=max_extension_atr,
        min_stop_atr=min_stop_atr,
        max_stop_atr=max_stop_atr,
        max_signals_per_day=max_per_day,
        max_open_positions=max_open_positions,
        max_daily_full_stops=max_daily_stops,
    )

    trix_monitor = NotifyingTrixAdxOutcomeMonitor(
        postgres,
        event_bus,
        ambiguous_candle_mode=settings.ambiguous_candle_mode,
        max_shadow_signals_per_cycle=200,
        trix_length=trix_length,
        trix_signal_length=trix_signal_length,
        trailing_atr_multiple=trailing_atr,
        break_even_buffer_rate=be_buffer,
        sell_fee_rate=settings.binance_sell_fee_rate,
        spread_rate=settings.binance_spread_rate,
        slippage_rate=settings.binance_slippage_rate,
    )

    old_ichimoku_monitor = None
    if _enabled("TRIX_FINISH_ICHIMOKU_OPEN_SIGNALS", "true"):
        old_ichimoku_monitor = NotifyingIchimokuOutcomeMonitor(
            postgres,
            event_bus,
            ambiguous_candle_mode=settings.ambiguous_candle_mode,
            max_shadow_signals_per_cycle=200,
            sell_fee_rate=settings.binance_sell_fee_rate,
            spread_rate=settings.binance_spread_rate,
            slippage_rate=settings.binance_slippage_rate,
        )
        logger.info(
            "Legacy open-signal management enabled runtime=%s; no new Ichimoku entries",
            ICHIMOKU_RUNTIME_VERSION,
        )

    position_monitor = CompositePositionMonitor(trix_monitor, old_ichimoku_monitor)

    notification = ReliableNotificationEngine(
        event_bus,
        enabled=True,
        max_message_length=settings.telegram_max_message_length,
        max_retries=settings.telegram_max_retries,
        postgres=postgres,
    )
    notification.subscribe()

    if settings.telegram_send_startup_message:
        notification.send_telegram(
            "📈 <b>TRIX + ADX TREND · PAPER ATTIVO</b>\n\n"
            f"Strategia: <b>{STRATEGY_NAME}</b>\n"
            "Mercati: <b>20 crypto/USD</b>\n"
            "Contesto: <b>4h</b> · ingresso: <b>1h</b>\n"
            "Filtro 4h: EMA50 &gt; EMA200, MACD positivo, ADX &gt; 25 e crescente\n"
            "Trigger 1h: TRIX 20 attraversa lo zero, volume confermato, prezzo non esteso\n"
            "Stop: strutturale fra 1,0 e 1,8 ATR\n"
            "A +1R: stop al pareggio netto comprensivo dei costi\n"
            "A +2R: chiusura del 50%\n"
            "Restante 50%: trailing 2,5 ATR, uscita TRIX&lt;0 o sotto EMA50\n"
            f"Massimo segnali: <b>{max_per_day}/giorno</b> · posizioni aperte: <b>{max_open_positions}</b>\n\n"
            "🧪 Solo PAPER TRADING. La precedente strategia Ichimoku non genera più ingressi."
        )

    HourAlignedTrixAdxScheduler(
        settings,
        collector,
        None,
        None,
        scanner,
        position_monitor,
    ).run_forever()


if __name__ == "__main__":
    main()
