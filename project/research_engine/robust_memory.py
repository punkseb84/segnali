"""Memory-bounded wrapper for robust walk-forward research."""
from __future__ import annotations

from project.research_engine.robust_service import RobustWalkForwardResearchEngine
from project.shared.memory import release_unused_memory


class MemoryEfficientRobustResearchEngine(RobustWalkForwardResearchEngine):
    def process_one_batch(self, sleep_after: bool = True):
        try:
            return super().process_one_batch(sleep_after=sleep_after)
        finally:
            rss_mb = release_unused_memory()
            if rss_mb is not None:
                self.logger.info("MEMORY_RELEASE phase=robust_research rss_mb=%.1f", rss_mb)
