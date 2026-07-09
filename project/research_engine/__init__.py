"""Phase 3 boundary: progressive PostgreSQL-backed Quant Research Engine."""

from project.research_engine.repository import ResearchCombination, ResearchRepository
from project.research_engine.service import ProgressiveResearchEngine, ResearchBatchResult

__all__ = ["ProgressiveResearchEngine", "ResearchBatchResult", "ResearchCombination", "ResearchRepository"]
