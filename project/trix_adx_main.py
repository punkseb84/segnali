"""Railway runtime for the only active strategy: TRIX V2 LONG/SHORT PAPER."""
from __future__ import annotations

import os
from dataclasses import replace

import project.notification_engine.service as notification_service
from project.capital_protection_migration import run_capital_protection_migration
from project.config.settings import load_settings
from project.database.migrations import run_migrations
from project.database.postgres import parse_postgres_connection_info, sanitize_postgres_error
from project.notification_engine.simple_formatters import format_simple_report_message
from project.reliable_notification import ReliableNotificationEngine
from project.shared.events import EventBus
from project.shared.logging import get_module_logger
from project.simple_main import apply_simple_policy, build_collector, build_postgres
from project.trix_adx_formatters import format_trix_adx_outcome_message, format_trix_adx_signal_message
from project.trix_adx_v2_monitor import TrixAdxV2Monitor
from project.trix_adx_v2_scanner import RUNTIME_VERSION, STRATEGY_NAME, TrixAdxV2Scanner
from project.trix_adx_v2_scheduler import QuarterHourTrixScheduler

TOP_20_PAIRS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD",
    "DOGE/USD", "LINK/USD", "AVAX/USD", "AAVE/USD", "LTC/USD",
    "BCH/USD", "TAO/USD", "DOT/USD", "XLM/USD", "TRX/USD",
    "ATOM/USD", "ETC/USD", "FIL/USD", "NEAR/USD", "UNI/USD",
]


def _retire_previous_runtime_signals(postgres: object) -> None:
    postgres.execute(
        """UPDATE signals.generated_signals
           SET status = 'EXPIRED',
               closed_at = COALESCE(closed_at, NOW()),
               outcome_resolution = 'REPLACED_BY_TRIX_V2_LONG_SHORT'
           WHERE status IN ('NEW', 'OPEN')
             AND COALESCE(score_breakdown->>'runtime_version', '') <> %s""",
        (RUNTIME_VERSION,),
    )


def main() -> None:
    settings = apply_simple_policy(load_settings())
    settings = replace(
        settings,
        collector_pairs=list(TOP_20_PAIRS),
        collector_timeframes=["15m", "1h"],
        operational_timeframe="15m",
        scheduler_collector_seconds=900,
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
    max_per_day = max(1, int(os.getenv("TRIX_MAX_PER_DAY", "4")))
    max_open_positions = max(1, int(os.getenv("TRIX_MAX_OPEN_POSITIONS", "2")))
    max_daily_stops = max(1, int(os.getenv("TRIX_MAX_DAILY_STOPS", "2")))

    logger.info("======================================")
    logger.info("PROJECT MAIN: ONLY TRIX V2 LONG/SHORT PAPER")
    logger.info("======================================")
    logger.info(
        "TRIX_V2_RUNTIME version=%s strategy=%s trigger=15m context=1h directions=LONG,SHORT pairs=%s paper=true",
        RUNTIME_VERSION, STRATEGY_NAME, len(settings.collector_pairs),
    )

    try:
        postgres = build_postgres(settings)
    except Exception as exc:
        logger.error("PostgreSQL connection failed: %s", sanitize_postgres_error(str(exc), settings.database_url))
        raise SystemExit(1) from exc

    logger.info("PostgreSQL connection: OK")
    logger.info("PostgreSQL %s", parse_postgres_connection_info(settings.database_url).display())
    run_migrations(postgres)
    run_capital_protection_migration(postgres)
    _retire_previous_runtime_signals(postgres)

    event_bus = EventBus()
    notification_service.format_signal_message = format_trix_adx_signal_message
    notification_service.format_report_message = format_simple_report_message
    notification_service.format_outcome_message = format_trix_adx_outcome_message

    collector = build_collector(settings, event_bus, postgres)
    scanner = TrixAdxV2Scanner(
        postgres, event_bus, settings.collector_pairs,
        exchange=settings.exchange_name,
        trade_notional_eur=settings.trade_notional_eur,
        buy_fee_rate=settings.binance_buy_fee_rate,
        sell_fee_rate=settings.binance_sell_fee_rate,
        spread_rate=settings.binance_spread_rate,
        slippage_rate=settings.binance_slippage_rate,
        quantity_step=settings.binance_quantity_step,
        min_qty=settings.binance_min_qty,
        min_notional_eur=settings.binance_min_notional_eur,
        signal_cooldown_minutes=max(60, settings.signal_cooldown_minutes),
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

    monitor = TrixAdxV2Monitor(
        postgres, event_bus,
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

    notification = ReliableNotificationEngine(
        event_bus, enabled=True,
        max_message_length=settings.telegram_max_message_length,
        max_retries=settings.telegram_max_retries,
        postgres=postgres,
    )
    notification.subscribe()

    if settings.telegram_send_startup_message:
        notification.send_telegram(
            "📈 <b>TRIX V2 LONG/SHORT · PAPER ATTIVO</b>\n\n"
            f"Strategia: <b>{STRATEGY_NAME}</b>\n"
            "Verifica direzione: <b>1h</b>\n"
            "Segnale operativo: <b>15m</b>\n"
            "Direzioni abilitate: <b>LONG e SHORT</b>\n"
            "Trigger: attraversamento TRIX dello zero, trend EMA/MACD e ADX crescente\n"
            "Gestione: stop strutturale, pareggio netto a +1R, 50% a +2R, trailing ATR\n\n"
            "🧪 Solo PAPER TRADING. Nessun ordine reale viene inviato."
        )

    QuarterHourTrixScheduler(settings, collector, None, None, scanner, monitor).run_forever()


if __name__ == "__main__":
    main()
