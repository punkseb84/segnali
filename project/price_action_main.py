"""Railway entrypoint for the pure price-action V7 paper scanner."""
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
from project.notification_engine.simple_formatters import format_simple_report_message
from project.operational_scheduler import OperationalMarketScheduler
from project.price_action_scanner import (
    HORIZONTAL_RETEST,
    PRICE_ACTION_RUNTIME_VERSION,
    TRENDLINE_RETEST,
    PurePriceActionScanner,
)
from project.reliable_notification import ReliableNotificationEngine
from project.shared.events import EventBus
from project.shared.logging import get_module_logger
from project.simple_main import apply_simple_policy, build_collector, build_postgres


def main() -> None:
    settings = apply_simple_policy(load_settings())
    logger = get_module_logger("system")
    paper_max_per_day = max(1, int(os.getenv("PRICE_ACTION_MAX_PER_DAY", "4")))
    paper_max_per_cycle = max(1, int(os.getenv("PRICE_ACTION_MAX_PER_CYCLE", "1")))
    paper_min_interval = max(
        15, int(os.getenv("PRICE_ACTION_MIN_INTERVAL_MINUTES", "60"))
    )
    paper_pair_cooldown = max(
        60, int(os.getenv("PRICE_ACTION_PAIR_COOLDOWN_MINUTES", "240"))
    )
    min_score = max(65.0, float(os.getenv("PRICE_ACTION_MIN_SCORE", "72")))
    min_net_rr = max(1.0, float(os.getenv("PRICE_ACTION_MIN_NET_RR", "1.15")))
    min_net_profit = max(
        0.10, float(os.getenv("PRICE_ACTION_MIN_NET_PROFIT_EUR", "0.15"))
    )
    max_net_loss = max(
        0.25, float(os.getenv("PRICE_ACTION_MAX_NET_LOSS_EUR", "1.00"))
    )

    logger.info("======================================")
    logger.info("PROJECT MAIN VERSION: 2026-07-23 PURE PRICE ACTION V7")
    logger.info("======================================")
    logger.info(
        "PURE_PRICE_ACTION_MODE runtime=%s pairs=%s strategies=%s "
        "oscillators=false ema=false macd=false rsi=false bollinger=false adx=false "
        "setup_timeframe=1h execution_timeframe=15m paper_max_day=%s "
        "paper_interval_minutes=%s pair_cooldown_minutes=%s min_score=%.1f "
        "min_net_rr=%.2f max_net_loss_eur=%.2f",
        PRICE_ACTION_RUNTIME_VERSION,
        len(settings.collector_pairs),
        [TRENDLINE_RETEST, HORIZONTAL_RETEST],
        paper_max_per_day,
        paper_min_interval,
        paper_pair_cooldown,
        min_score,
        min_net_rr,
        max_net_loss,
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
    scanner = PurePriceActionScanner(
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
        max_net_loss_eur=max_net_loss,
        paper_max_per_day=paper_max_per_day,
        paper_max_per_cycle=paper_max_per_cycle,
        paper_min_interval_minutes=paper_min_interval,
        paper_pair_cooldown_minutes=paper_pair_cooldown,
        swing_window=int(os.getenv("PRICE_ACTION_SWING_WINDOW", "2")),
        min_trendline_touches=int(
            os.getenv("PRICE_ACTION_MIN_TRENDLINE_TOUCHES", "3")
        ),
        min_horizontal_touches=int(
            os.getenv("PRICE_ACTION_MIN_HORIZONTAL_TOUCHES", "2")
        ),
        retest_window_15m=int(
            os.getenv("PRICE_ACTION_RETEST_WINDOW_15M", "6")
        ),
        enable_daily_signal_report=settings.enable_daily_signal_report,
        daily_signal_report_hours=settings.daily_signal_report_hours,
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
            "📐 <b>PURE PRICE ACTION V7 ATTIVO</b>\n\n"
            "Setup: <b>trendline breakout/retest</b> e <b>livello orizzontale breakout/retest</b>\n"
            "Analisi: <b>1h</b> · ingresso e monitoraggio: <b>15m</b>\n"
            "Usati soltanto: <b>OHLC, swing high/low, trendline, supporti, resistenze e candele di rifiuto</b>\n"
            "Disattivati: <b>RSI, MACD, EMA, ADX, Bollinger e filtri oscillatori</b>\n"
            f"R/R netto minimo: <b>{min_net_rr:.2f}</b>\n"
            f"Massimo segnali paper al giorno: <b>{paper_max_per_day}</b>\n\n"
            "Il report diagnostico continuerà a indicare perché un setup non è stato inviato.\n"
            "⚠️ Tutti i segnali restano PAPER finché l'edge non è dimostrato."
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
