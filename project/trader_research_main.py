"""One-off worker for public Hyperliquid trader research.

Run separately from the always-on V6 service:
    python -m project.trader_research_main
"""
from __future__ import annotations

import json
import os

from project.config.settings import load_settings
from project.database.migrations import run_migrations
from project.shared.logging import get_module_logger
from project.simple_main import apply_simple_policy, build_postgres
from project.trader_research.paged_client import PagedHyperliquidInfoClient
from project.trader_research.repository import TraderResearchRepository
from project.trader_research.service import DEFAULT_RESEARCH_WALLET
from project.trader_research.strict_service import StrictTraderResearchService


def main() -> None:
    settings = apply_simple_policy(load_settings())
    logger = get_module_logger("trader_research_worker")
    wallet = os.getenv("TRADER_RESEARCH_WALLET", DEFAULT_RESEARCH_WALLET).strip()
    lookback_days = max(7, int(os.getenv("TRADER_RESEARCH_LOOKBACK_DAYS", "120")))
    max_fills = min(10_000, max(100, int(os.getenv("TRADER_RESEARCH_MAX_FILLS", "10000"))))
    timeout_seconds = max(5.0, float(os.getenv("TRADER_RESEARCH_HTTP_TIMEOUT", "20")))
    max_retries = max(1, int(os.getenv("TRADER_RESEARCH_HTTP_RETRIES", "3")))

    logger.info("======================================")
    logger.info("PUBLIC TRADER RESEARCH VERSION: 2026-07-20 STRICT V1")
    logger.info("======================================")
    logger.info(
        "TRADER_RESEARCH_START wallet=%s lookback_days=%s max_fills=%s "
        "source=hyperliquid_public feature_exchange=%s live_runtime_unchanged=true",
        wallet,
        lookback_days,
        max_fills,
        settings.exchange_name,
    )

    postgres = build_postgres(settings)
    run_migrations(postgres)
    repository = TraderResearchRepository(postgres)
    api = PagedHyperliquidInfoClient(
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )
    service = StrictTraderResearchService(
        repository,
        api,
        market_exchange=settings.exchange_name,
    )
    summary = service.run(
        wallet,
        lookback_days=lookback_days,
        max_fills=max_fills,
    )
    logger.info(
        "TRADER_RESEARCH_RESULT wallet=%s fills=%s cycles=%s features=%s "
        "eligible_long_playbooks=%s",
        summary["wallet"],
        summary["fills"],
        summary["cycles_total"],
        summary["features"],
        summary["eligible_long_playbooks"],
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
