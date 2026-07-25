"""Railway runtime for the only active Ichimoku cloud breakout strategy."""
from __future__ import annotations

import os
from dataclasses import replace

import project.notification_engine.service as notification_service
from project.capital_protection_migration import run_capital_protection_migration
from project.config.settings import load_settings
from project.daily_paper_formatters import (
    format_daily_paper_outcome_message,
    format_daily_paper_signal_message,
)
from project.database.migrations import run_migrations
from project.database.postgres import parse_postgres_connection_info, sanitize_postgres_error
from project.ichimoku_daily_limited_scanner import DailyLimitedIchimokuScanner
from project.ichimoku_position_monitor import IchimokuPositionMonitor
from project.ichimoku_scanner import RUNTIME_VERSION, STRATEGY_NAME
from project.notification_engine.simple_formatters import format_simple_report_message
from project.operational_scheduler import OperationalMarketScheduler
from project.reliable_notification import ReliableNotificationEngine
from project.shared.events import EventBus
from project.shared.logging import get_module_logger
from project.simple_main import apply_simple_policy, build_collector, build_postgres


TOP_20_PAIRS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD",
    "DOGE/USD", "LINK/USD", "AVAX/USD", "AAVE/USD", "LTC/USD",
    "BCH/USD", "TAO/USD", "DOT/USD", "XLM/USD", "TRX/USD",
    "ATOM/USD", "ETC/USD", "FIL/USD", "NEAR/USD", "UNI/USD",
]


def _expire_previous_runtime_signals(postgres: object) -> None:
    postgres.execute(
        """UPDATE signals.generated_signals
        SET status = 'EXPIRED',
            closed_at = COALESCE(closed_at, NOW()),
            outcome_resolution = COALESCE(
                outcome_resolution,
                'REPLACED_BY_ICHIMOKU_RUNTIME'
            )
        WHERE status IN ('NEW', 'OPEN')
          AND COALESCE(score_breakdown->>'runtime_version', '') <> %s""",
        (RUNTIME_VERSION,),
    )


def main() -> None:
    settings = apply_simple_policy(load_settings())
    settings = replace(
        settings,
        collector_pairs=list(TOP_20_PAIRS),
        collector_timeframes=["1h"],
        operational_timeframe="1h",
    )
    logger = get_module_logger("system")

    tenkan_period = int(os.getenv("ICHIMOKU_TENKAN_PERIOD", "9"))
    kijun_period = int(os.getenv("ICHIMOKU_KIJUN_PERIOD", "26"))
    senkou_b_period = int(os.getenv("ICHIMOKU_SENKOU_B_PERIOD", "52"))
    displacement = int(os.getenv("ICHIMOKU_DISPLACEMENT", "26"))
    atr_period = int(os.getenv("ICHIMOKU_ATR_PERIOD", "14"))
    atr_stop = float(os.getenv("ICHIMOKU_ATR_STOP_MULTIPLE", "1.5"))
    reference_target_atr = float(os.getenv("ICHIMOKU_REFERENCE_TARGET_ATR", "3.0"))
    min_score = float(os.getenv("ICHIMOKU_MIN_SCORE", "70"))
    min_reference_profit = float(os.getenv("ICHIMOKU_MIN_REFERENCE_PROFIT_EUR", "0.10"))
    max_per_cycle = max(1, int(os.getenv("ICHIMOKU_MAX_PER_CYCLE", "2")))
    max_per_day = max(1, int(os.getenv("ICHIMOKU_MAX_PER_DAY", "10")))

    logger.info("======================================")
    logger.info("PROJECT MAIN: ONLY ICHIMOKU STRATEGY")
    logger.info("======================================")
    logger.info(
        "ICHIMOKU_RUNTIME version=%s strategy=%s timeframe=1h pairs=%s "
        "tenkan=%s kijun=%s senkou_b=%s displacement=%s atr=%s "
        "atr_stop=%.2f max_per_day=%s telegram=true",
        RUNTIME_VERSION,
        STRATEGY_NAME,
        len(settings.collector_pairs),
        tenkan_period,
        kijun_period,
        senkou_b_period,
        displacement,
        atr_period,
        atr_stop,
        max_per_day,
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
    _expire_previous_runtime_signals(postgres)

    event_bus = EventBus()
    notification_service.format_signal_message = format_daily_paper_signal_message
    notification_service.format_report_message = format_simple_report_message
    notification_service.format_outcome_message = format_daily_paper_outcome_message

    collector = build_collector(settings, event_bus, postgres)
    scanner = DailyLimitedIchimokuScanner(
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
        signal_cooldown_minutes=max(120, settings.signal_cooldown_minutes),
        min_signal_score=min_score,
        min_net_rr=0.0,
        min_net_profit_eur=min_reference_profit,
        max_signals_per_cycle=max_per_cycle,
        max_signals_per_day=max_per_day,
        enable_daily_signal_report=settings.enable_daily_signal_report,
        daily_signal_report_hours=settings.daily_signal_report_hours,
        tenkan_period=tenkan_period,
        kijun_period=kijun_period,
        senkou_b_period=senkou_b_period,
        displacement=displacement,
        atr_period=atr_period,
        atr_stop_multiple=atr_stop,
        reference_target_atr=reference_target_atr,
    )
    position_monitor = IchimokuPositionMonitor(
        postgres,
        event_bus,
        ambiguous_candle_mode=settings.ambiguous_candle_mode,
        max_shadow_signals_per_cycle=200,
        tenkan_period=tenkan_period,
        kijun_period=kijun_period,
        senkou_b_period=senkou_b_period,
        displacement=displacement,
        sell_fee_rate=settings.binance_sell_fee_rate,
        spread_rate=settings.binance_spread_rate,
        slippage_rate=settings.binance_slippage_rate,
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
            "☁️ <b>ICHIMOKU CLOUD BREAKOUT ATTIVO</b>\n\n"
            f"Unica strategia: <b>{STRATEGY_NAME}</b>\n"
            "Mercati: <b>20 crypto/USD</b>\n"
            "Timeframe: <b>1h</b>\n"
            "Ingresso: prima chiusura sopra tutta la nuvola\n"
            "Conferma: <b>Tenkan Sen &gt; Kijun Sen</b>\n"
            f"Stop iniziale: <b>{atr_stop:.2f} ATR</b>\n"
            "Uscita: chiusura dentro/sotto la nuvola oppure incrocio ribassista Tenkan/Kijun\n"
            f"Massimo segnali Telegram: <b>{max_per_day} al giorno</b>\n\n"
            "Le strategie precedenti sono escluse dal runtime e i vecchi segnali aperti sono scaduti.\n"
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
