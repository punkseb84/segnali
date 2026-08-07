"""Clean Railway runtime for the Oliver Velez style 15m PAPER strategy."""
from __future__ import annotations

from dataclasses import replace

import project.notification_engine.service as notification_service
import project.relative_strength_monitor as monitor_module
from project.capital_protection_migration import run_capital_protection_migration
from project.config.settings import load_settings
from project.database.migrations import run_migrations
from project.database.postgres import parse_postgres_connection_info, sanitize_postgres_error
from project.reliable_notification import ReliableNotificationEngine
from project.relative_strength_v3_monitor import RelativeStrengthV3Monitor
from project.shared.events import EventBus
from project.shared.logging import get_module_logger
from project.simple_main import apply_simple_policy, build_collector, build_postgres
from project.velez_15m_scanner import RUNTIME_VERSION, Velez15mScanner
from project.velez_formatters import format_velez_outcome, format_velez_signal
from project.velez_scheduler import Velez15mScheduler
from project.velez_universe import resolve_top_market_cap_pairs

PUBLIC_BUDGET_EUR = 100.0
MAX_OPEN = 4
PER_TRADE_EUR = PUBLIC_BUDGET_EUR / MAX_OPEN


def _retire_previous_open_signals(postgres: object) -> None:
    postgres.execute(
        """UPDATE signals.generated_signals
           SET status='EXPIRED', closed_at=COALESCE(closed_at,NOW()),
               outcome_resolution='REPLACED_BY_VELEZ_15M_RUNTIME'
           WHERE status IN ('NEW','OPEN')
             AND COALESCE(score_breakdown->>'runtime_version','') <> %s""",
        (RUNTIME_VERSION,),
    )


def main() -> None:
    pairs = resolve_top_market_cap_pairs(20)
    settings = apply_simple_policy(load_settings())
    settings = replace(
        settings,
        collector_pairs=pairs,
        collector_timeframes=["15m"],
        operational_timeframe="15m",
        scheduler_collector_seconds=900,
        telegram_send_startup_message=False,
        enable_daily_signal_report=False,
    )
    logger = get_module_logger("system")
    logger.info("======================================")
    logger.info("PROJECT MAIN: VELEZ 15M PAPER ONLY")
    logger.info("======================================")
    logger.info(
        "VELEZ_RUNTIME version=%s timeframe=15m pairs=%s budget=%.2f per_trade=%.2f "
        "relative_strength=false trix=false ichimoku=false paper=true",
        RUNTIME_VERSION,
        pairs,
        PUBLIC_BUDGET_EUR,
        PER_TRADE_EUR,
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
    _retire_previous_open_signals(postgres)

    # The generic monitor is reused only for PAPER lifecycle and reads this runtime id.
    monitor_module.RUNTIME_VERSION = RUNTIME_VERSION

    event_bus = EventBus()
    notification_service.format_signal_message = format_velez_signal
    notification_service.format_outcome_message = format_velez_outcome

    collector = build_collector(settings, event_bus, postgres)
    scanner = Velez15mScanner(
        postgres,
        event_bus,
        settings.collector_pairs,
        exchange=settings.exchange_name,
        trade_notional_eur=PER_TRADE_EUR,
        buy_fee_rate=settings.binance_buy_fee_rate,
        sell_fee_rate=settings.binance_sell_fee_rate,
        spread_rate=settings.binance_spread_rate,
        slippage_rate=settings.binance_slippage_rate,
        quantity_step=settings.binance_quantity_step,
        min_qty=settings.binance_min_qty,
        min_notional_eur=settings.binance_min_notional_eur,
        signal_cooldown_minutes=60,
        max_signals_per_cycle=2,
        max_signals_per_day=8,
        max_open_positions=MAX_OPEN,
        max_per_pair_day=2,
        target_r=1.5,
        max_hold_candles=12,
        pullback_bars=3,
    )
    monitor = RelativeStrengthV3Monitor(
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

    Velez15mScheduler(settings, collector, None, None, scanner, monitor).run_forever()


if __name__ == "__main__":
    main()
