"""Memory-trimmed Railway wrapper for the progressive research engine."""
from __future__ import annotations

from project.research_engine.service import ProgressiveResearchEngine, ResearchBatchResult
from project.shared.memory import release_unused_memory


class MemoryEfficientResearchEngine(ProgressiveResearchEngine):
    """Preserve research throughput while returning transient pandas memory."""

    def process_one_batch(self, sleep_after: bool = True) -> ResearchBatchResult:
        try:
            return super().process_one_batch(sleep_after=sleep_after)
        finally:
            rss_mb = release_unused_memory()
            if rss_mb is not None:
                self.logger.info("MEMORY_RELEASE phase=research rss_mb=%.1f", rss_mb)
