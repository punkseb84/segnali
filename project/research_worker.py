"""One-shot robust research worker with isolated historical backfill."""
from __future__ import annotations

import os

from project.config.settings import load_settings
from project.data_collector.binance_history import BinanceHistoricalKlineClient
from project.data_collector.repository import MarketDataRepository
from project.database.migrations import run_migrations
from project.database.postgres import PostgresClient, PostgresConfig
from project.notification_engine.service import NotificationEngine
from project.research_engine.robust_memory import MemoryEfficientRobustResearchEngine
from project.research_engine.sourced_repository import (
    BINANCE_RESEARCH_EXCHANGE,
    ROBUST_RESEARCH_VERSION_V2,
    SourcedRobustResearchRepository,
)
from project.shared.events import EventBus
from project.shared.logging import get_module_logger
from project.shared.memory import release_unused_memory


def backfill_research_history(settings, postgres, logger) -> dict[str, int]:
    timeframe = settings.operational_timeframe
    target = max(1800, int(os.getenv("ROBUST_RESEARCH_OHLC_LIMIT", "3000")))
    market_repository = MarketDataRepository(postgres, BINANCE_RESEARCH_EXCHANGE)
    client = BinanceHistoricalKlineClient(
        api_base=os.getenv(
            "BINANCE_RESEARCH_API_BASE", "https://data-api.binance.vision"
        ),
        timeout_seconds=int(os.getenv("BINANCE_RESEARCH_TIMEOUT_SECONDS", "20")),
        max_retries=int(os.getenv("BINANCE_RESEARCH_MAX_RETRIES", "3")),
    )
    completed = 0
    failed = 0
    minimum_rows = 0
    for pair in settings.collector_pairs:
        try:
            market_repository.upsert_pair(pair)
            market_repository.upsert_timeframe(timeframe)
            before = market_repository.verify_integrity(pair, timeframe).rows
            if before < target:
                frame = client.fetch_recent_closed_klines(pair, timeframe, target)
                market_repository.insert_ohlc(pair, timeframe, frame)
            after = market_repository.verify_integrity(pair, timeframe).rows
            minimum_rows = after if minimum_rows == 0 else min(minimum_rows, after)
            if after >= 1800:
                completed += 1
            else:
                failed += 1
            logger.info(
                "RESEARCH_HISTORY pair=%s timeframe=%s before=%s after=%s target=%s source=%s",
                pair,
                timeframe,
                before,
                after,
                target,
                BINANCE_RESEARCH_EXCHANGE,
            )
        except Exception as exc:
            failed += 1
            logger.exception(
                "RESEARCH_HISTORY_FAILED pair=%s timeframe=%s error=%s",
                pair,
                timeframe,
                exc,
            )
    return {
        "completed_pairs": completed,
        "failed_pairs": failed,
        "minimum_rows": minimum_rows,
        "target_rows": target,
    }


def main() -> None:
    os.environ.setdefault("ROBUST_MIN_CANDLES", "1800")
    os.environ.setdefault("ROBUST_MIN_TOTAL_TRADES", "30")
    os.environ.setdefault("ROBUST_MIN_TRAIN_TRADES", "18")
    os.environ.setdefault("ROBUST_MIN_TEST_TRADES", "10")
    os.environ.setdefault("ROBUST_MIN_TRAIN_PROFIT_FACTOR", "1.05")
    os.environ.setdefault("ROBUST_MIN_TEST_PROFIT_FACTOR", "1.15")
    os.environ.setdefault("ROBUST_MIN_TEST_EXPECTANCY_EUR", "0.02")
    os.environ.setdefault("ROBUST_RESEARCH_OHLC_LIMIT", "3000")

    settings = load_settings()
    mode = os.getenv("LOW_COST_RESEARCH_WORKER_MODE", "periodic")
    logger = get_module_logger("research-worker")
    logger.info(
        "RESEARCH_V2_WORKER start mode=%s batch_size=%s runtime_minutes=%s "
        "ohlc_limit=%s version=%s",
        mode,
        settings.research_batch_size,
        settings.max_research_runtime_minutes,
        os.environ["ROBUST_RESEARCH_OHLC_LIMIT"],
        ROBUST_RESEARCH_VERSION_V2,
    )
    postgres = PostgresClient(PostgresConfig(settings.database_url))
    postgres.test_connection()
    run_migrations(postgres)

    history = backfill_research_history(settings, postgres, logger)
    event_bus = EventBus()
    repository = SourcedRobustResearchRepository(
        postgres,
        market_exchange=BINANCE_RESEARCH_EXCHANGE,
        research_version=ROBUST_RESEARCH_VERSION_V2,
    )
    engine = MemoryEfficientRobustResearchEngine(
        repository,
        event_bus,
        batch_size=settings.research_batch_size,
        max_runtime_minutes=settings.max_research_runtime_minutes,
        sleep_between_batches_seconds=0,
        batch_interval_seconds=max(10800, settings.railway_light_research_seconds),
        priority_timeframe=settings.operational_timeframe,
        trade_notional_eur=settings.trade_notional_eur,
        buy_fee_rate=settings.binance_buy_fee_rate,
        sell_fee_rate=settings.binance_sell_fee_rate,
        spread_rate=settings.binance_spread_rate,
        slippage_rate=settings.binance_slippage_rate,
        probability_horizon_candles=settings.probability_horizon_candles,
    )
    engine.research_version = ROBUST_RESEARCH_VERSION_V2
    engine.seed_combinations(
        settings.collector_pairs,
        [settings.operational_timeframe],
    )
    result = engine.process_one_batch(sleep_after=False)
    diagnostics = repository.fetch_candidate_diagnostics(
        timeframe=settings.operational_timeframe
    )
    rss_mb = release_unused_memory()
    logger.info(
        "RESEARCH_V2_WORKER completed mode=%s status=%s processed=%s "
        "history_pairs=%s history_failed=%s min_rows=%s eligible=%s done=%s "
        "pending=%s max_candles=%s max_trades=%s rss_mb=%s",
        mode,
        result.status,
        result.processed,
        history["completed_pairs"],
        history["failed_pairs"],
        history["minimum_rows"],
        diagnostics.get("eligible_results", 0),
        diagnostics.get("done_combinations", 0),
        diagnostics.get("pending_combinations", 0),
        diagnostics.get("max_candles", 0),
        diagnostics.get("max_trades", 0),
        f"{rss_mb:.1f}" if rss_mb is not None else "unknown",
    )

    if mode == "startup" and settings.enable_notification_engine:
        notification = NotificationEngine(
            event_bus,
            enabled=True,
            max_message_length=settings.telegram_max_message_length,
            max_retries=settings.telegram_max_retries,
            postgres=postgres,
        )
        eligible = int(diagnostics.get("eligible_results", 0) or 0)
        pending = int(diagnostics.get("pending_combinations", 0) or 0)
        done = int(diagnostics.get("done_combinations", 0) or 0)
        max_candles = int(diagnostics.get("max_candles", 0) or 0)
        max_trades = int(diagnostics.get("max_trades", 0) or 0)
        notification.send_telegram(
            "🧪 <b>RICERCA V2 COMPLETATA</b>\n\n"
            f"• Coppie con storico sufficiente: <b>{history['completed_pairs']}</b>\n"
            f"• Coppie non disponibili: <b>{history['failed_pairs']}</b>\n"
            f"• Candele massime utilizzate: <b>{max_candles}</b>\n"
            f"• Trade massimi prodotti: <b>{max_trades}</b>\n"
            f"• Configurazioni elaborate: <b>{done}</b>\n"
            f"• Strategie robuste disponibili: <b>{eligible}</b>\n"
            f"• Configurazioni residue: <b>{pending}</b>\n\n"
            "La ricerca usa storico separato dal feed Kraken live e il processo ha liberato la RAM."
        )


if __name__ == "__main__":
    main()
