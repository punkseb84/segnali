"""Strategy Engine: generate candidate signal events from validated research and market decisions."""
from project.strategy_engine.service import GeneratedSignal, StrategyEngine
from project.strategy_engine.economic_guard import EconomicallyGuardedProbabilisticStrategyEngine
from project.strategy_engine import probabilistic_service as _probabilistic_service

# Keep the existing import path used by project.main, while enforcing the economic
# minimums for operative A/B signals. This avoids changing the Railway entrypoint.
_probabilistic_service.ProbabilisticStrategyEngine = EconomicallyGuardedProbabilisticStrategyEngine
ProbabilisticStrategyEngine = EconomicallyGuardedProbabilisticStrategyEngine

__all__ = [
    "GeneratedSignal",
    "StrategyEngine",
    "ProbabilisticStrategyEngine",
    "EconomicallyGuardedProbabilisticStrategyEngine",
]
