"""Railway entrypoint with synchronized lifecycle and robust OOS research."""
from __future__ import annotations

import os

# Safe defaults for €100 dry-run signals. Railway variables can still override them.
os.environ.setdefault("ROBUST_RESEARCH_OHLC_LIMIT", "5000")
os.environ.setdefault("ROBUST_MIN_CANDLES", "1800")
os.environ.setdefault("ROBUST_MIN_TOTAL_TRADES", "30")
os.environ.setdefault("ROBUST_MIN_TRAIN_TRADES", "18")
os.environ.setdefault("ROBUST_MIN_TEST_TRADES", "10")
os.environ.setdefault("ROBUST_MIN_TRAIN_PROFIT_FACTOR", "1.05")
os.environ.setdefault("ROBUST_MIN_TEST_PROFIT_FACTOR", "1.15")
os.environ.setdefault("ROBUST_MIN_TEST_EXPECTANCY_EUR", "0.02")

import project.main as modular_main
import project.notification_engine.service as notification_service
from project.notification_engine.robust_formatters import (
    format_robust_signal_message,
    format_robust_watchlist_message,
)
from project.research_engine.robust_memory import MemoryEfficientRobustResearchEngine
from project.research_engine.robust_repository import RobustResearchRepository
from project.synchronized_scheduler import SynchronizedPlatformScheduler
from project.strategy_engine.robust_operational import RobustEdgeOperationalStrategyEngine


# Reuse the stable bootstrap while replacing the runtime components that determine
# research validity, signal eligibility and collector/strategy ordering.
modular_main.PlatformScheduler = SynchronizedPlatformScheduler
modular_main.ResearchRepository = RobustResearchRepository
modular_main.MemoryEfficientResearchEngine = MemoryEfficientRobustResearchEngine
modular_main.ProgressiveOperationalStrategyEngine = RobustEdgeOperationalStrategyEngine
notification_service.format_signal_message = format_robust_signal_message
notification_service.format_watchlist_message = format_robust_watchlist_message


if __name__ == "__main__":
    modular_main.main()
