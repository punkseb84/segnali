"""Railway runtime for the single Donchian EMA200 ADX ATR paper strategy."""
from __future__ import annotations

import os

import project.notification_engine.service as notification_service
from project.capital_protection_migration import run_capital_protection_migration
from project.config.settings import load_settings
from project.daily_paper_formatters import (
    format_daily_paper_outcome_message,
    format_daily_paper_signal_message,
)
from project.daily_paper_position_monitor import DailyPaperPositionMonitor
from project.database.migrations import run_migrations
from project.database.postgres import parse_postgres_connection_info, sanitize_postgres_error
from project.donchian_scanner import DonchianBreakoutScanner, RUNTIME_VERSION, STRATEGY_NAME
from project.notification_engine.simple_formatters import format_simple_report_message
from project.operational_scheduler import OperationalMarketScheduler
from project.reliable_notification import ReliableNotificationEngine
from project.shared.events import EventBus
from project.shared.logging import get_module_logger
from project.simple_main import apply_simple_policy, build_collector, build_postgres


def main() -> None:
    settings = apply_simple_policy(load_settings())
    logger = get_module_logger("system")

    donchian_period = int(os.getenv("DONCHIAN_PERIOD", "20"))
    ema_period = int(os.getenv("DONCHIAN_EMA_PERIOD", "200"))
    adx_period = int(os.getenv("DONCHIAN_ADX_PERIOD", "14"))
    adx_min = float(os.getenv("DONCHIAN_ADX_MIN", "25"))
    atr_period = int(os.getenv("DONCHIAN_ATR_PERIOD", "14"))
    atr_stop = float(os.getenv("DONCHIAN_ATR_STOP_MULTIPLE", "1.5"))
    atr_target = float(os.getenv("DONCHIAN_ATR_TARGET_MULTIPLE", "3.0"))
    min_score = float(os.getenv("DONCHIAN_MIN_SCORE", "70"))
    min_net_rr = float(os.getenv("DONCHIAN_MIN_NET_RR", "1.20"))
    min_net_profit = float(os.getenv("DONCHIAN_MIN_NET_PROFIT_EUR", "0.10"))
    max_per_cycle = max(1, int(os.getenv("DONCHIAN_MAX_PER_CYCLE", "2")))

    logger.info("======================================")
    logger.info("PROJECT MAIN: SINGLE DONCHIAN STRATEGY")
    logger.info("======================================")
    logger.info(
        "DONCHIAN_RUNTIME version=%s strategy=%s timeframe=15m pairs=%s "
        "donchian=%s ema=%s adx_period=%s adx_min=%.1f atr_period=%s "
        "atr_stop=%.2f atr_target=%.2f",
        RUNTIME_VERSION,
        STRATEGY_NAME,
        len(settings.collector_pairs),
        donchian_period,
        ema_period,
        adx_period,
        adx_min,
        atr_period,
        atr_stop,
        atr_target,
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
    logger.info(
        "PostgreSQL %s",
        parse_postgres_connection_info(settings.database_url).display(),
    )
    run_migrations(postgres)
    run_capital_protection_migration(postgres)

    event_bus = EventBus()
    notification_service.format_signal_message = format_daily_paper_signal_message
    notification_service.format_report_message = format_simple_report_message
    notification_service.format_outcome_message = format_daily_paper_outcome_message

    collector = build_collector(settings, event_bus, postgres)
    scanner = DonchianBreakoutScanner(
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
        signal_cooldown_minutes=max(60, settings.signal_cooldown_minutes),
        min_signal_score=min_score,
        min_net_rr=min_net_rr,
        min_net_profit_eur=min_net_profit,
        max_signals_per_cycle=max_per_cycle,
        enable_daily_signal_report=settings.enable_daily_signal_report,
        daily_signal_report_hours=settings.daily_signal_report_hours,
        donchian_period=donchian_period,
        ema_period=ema_period,
        adx_period=adx_period,
        adx_min=adx_min,
        atr_period=atr_period,
        atr_stop_multiple=atr_stop,
        atr_target_multiple=atr_target,
    )
    position_monitor = DailyPaperPositionMonitor(
        postgres,
        event_bus,
        ambiguous_candle_mode=settings.ambiguous_candle_mode,
        max_shadow_signals_per_cycle=200,
    )

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
            "🚀 <b>DONCHIAN BREAKOUT ATTIVO</b>\n\n"
            f"Strategia unica: <b>{STRATEGY_NAME}</b>\n"
            f"Timeframe: <b>15m</b>\n"
            f"Ingresso: chiusura sopra Donchian <b>{donchian_period}</b>\n"
            f"Filtro trend: prezzo sopra EMA <b>{ema_period}</b>\n"
            f"Filtro forza: ADX({adx_period}) ≥ <b>{adx_min:.1f}</b> e +DI &gt; -DI\n"
            f"Stop: <b>{atr_stop:.2f} ATR</b> · Target: <b>{atr_target:.2f} ATR</b>\n\n"
            "Tutte le strategie precedenti sono escluse dal runtime.\n"
            "⚠️ Segnali PAPER fino a validazione forward."
        )

    OperationalMarketScheduler(
        settings,
        collector,
        None,
        None,
        scanner,
        position_monitor,
    ).run_forever()


if __name__ == "__main__":
    main()
