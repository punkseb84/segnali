"""Railway runtime for the only active strategy: relative strength vs BTC."""
from __future__ import annotations

import os
from dataclasses import replace

import project.notification_engine.service as notification_service
from project.capital_protection_migration import run_capital_protection_migration
from project.config.settings import load_settings
from project.database.migrations import run_migrations
from project.database.postgres import parse_postgres_connection_info, sanitize_postgres_error
from project.notification_engine.simple_formatters import format_simple_report_message
from project.relative_strength_formatters import format_relative_strength_outcome, format_relative_strength_signal
from project.relative_strength_monitor import RelativeStrengthMonitor
from project.relative_strength_scanner import RUNTIME_VERSION, STRATEGY_NAME, RelativeStrengthScanner
from project.relative_strength_scheduler import RelativeStrengthScheduler
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


def _retire_all_previous_strategies(postgres: object) -> None:
    postgres.execute(
        """UPDATE signals.generated_signals
           SET status = 'EXPIRED',
               closed_at = COALESCE(closed_at, NOW()),
               outcome_resolution = 'REPLACED_BY_RELATIVE_STRENGTH_RUNTIME'
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

    strongest = max(1, int(os.getenv("RS_STRONGEST_COUNT", "3")))
    weakest = max(1, int(os.getenv("RS_WEAKEST_COUNT", "3")))
    max_per_cycle = max(1, int(os.getenv("RS_MAX_PER_CYCLE", "3")))
    max_per_day = max(1, int(os.getenv("RS_MAX_PER_DAY", "12")))
    max_open = max(1, int(os.getenv("RS_MAX_OPEN_POSITIONS", "4")))
    max_pair_day = max(1, int(os.getenv("RS_MAX_PER_PAIR_DAY", "2")))
    min_abs_score = float(os.getenv("RS_MIN_ABS_SCORE", "0.35"))
    volume_min = float(os.getenv("RS_VOLUME_RATIO_MIN", "0.70"))
    stop_atr = float(os.getenv("RS_STOP_ATR", "1.20"))
    target_r = float(os.getenv("RS_TARGET_R", "1.50"))
    max_hold = max(1, int(os.getenv("RS_MAX_HOLD_CANDLES", "8")))

    logger.info("======================================")
    logger.info("PROJECT MAIN: ONLY RELATIVE STRENGTH PAPER")
    logger.info("======================================")
    logger.info(
        "RELATIVE_STRENGTH_RUNTIME version=%s strategy=%s benchmark=BTC/USD context=1h trigger=15m "
        "directions=LONG,SHORT strongest=%s weakest=%s max_per_day=%s max_open=%s paper=true trix=false",
        RUNTIME_VERSION, STRATEGY_NAME, strongest, weakest, max_per_day, max_open,
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
    _retire_all_previous_strategies(postgres)
    logger.info("All previous NEW/OPEN TRIX, Ichimoku and legacy signals retired")

    event_bus = EventBus()
    notification_service.format_signal_message = format_relative_strength_signal
    notification_service.format_outcome_message = format_relative_strength_outcome
    notification_service.format_report_message = format_simple_report_message

    collector = build_collector(settings, event_bus, postgres)
    scanner = RelativeStrengthScanner(
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
        signal_cooldown_minutes=60,
        min_signal_score=0.0,
        min_net_rr=0.0,
        min_net_profit_eur=0.0,
        max_signals_per_cycle=max_per_cycle,
        enable_daily_signal_report=settings.enable_daily_signal_report,
        daily_signal_report_hours=settings.daily_signal_report_hours,
        strongest_count=strongest,
        weakest_count=weakest,
        max_signals_per_day=max_per_day,
        max_open_positions=max_open,
        max_per_pair_day=max_pair_day,
        min_abs_score=min_abs_score,
        volume_ratio_min=volume_min,
        stop_atr=stop_atr,
        target_r=target_r,
        max_hold_candles=max_hold,
    )

    monitor = RelativeStrengthMonitor(
        postgres,
        event_bus,
        ambiguous_candle_mode=settings.ambiguous_candle_mode,
        max_shadow_signals_per_cycle=300,
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
            "📊 <b>FORZA RELATIVA CRYPTO vs BTC · PAPER ATTIVA</b>\n\n"
            f"Strategia unica: <b>{STRATEGY_NAME}</b>\n"
            "Benchmark: <b>BTC/USD</b>\n"
            "Ranking: rendimento relativo 4h, 1h e 15m + rapporto asset/BTC + volume\n"
            f"LONG: <b>{strongest} crypto più forti</b> · SHORT: <b>{weakest} più deboli</b>\n"
            "Conferma: candela 15m e posizione rispetto a EMA20\n"
            f"Gestione: stop <b>{stop_atr:.2f} ATR</b> · target <b>{target_r:.2f}R</b>\n"
            f"Uscita anticipata: inversione RS o dopo <b>{max_hold} candele</b>\n"
            f"Limiti: <b>{max_per_day} segnali/giorno</b> · {max_pair_day} per coppia · {max_open} aperti\n\n"
            "🧪 Solo PAPER TRADING. TRIX e Ichimoku sono disattivati."
        )

    RelativeStrengthScheduler(
        settings, collector, None, None, scanner, monitor,
    ).run_forever()


if __name__ == "__main__":
    main()
