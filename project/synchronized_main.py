"""Railway entrypoint with synchronized lifecycle and robust OOS research."""
from __future__ import annotations

import project.main as modular_main
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


if __name__ == "__main__":
    modular_main.main()
