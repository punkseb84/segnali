from project.research_engine.repository import ResearchCombination
from project.research_engine.service import ProgressiveResearchEngine
from project.shared.events import EventBus, EventType


class FakeResearchRepository:
    def __init__(self, existing_count=None):
        self.seeded = []
        self.existing_count = existing_count
        self.marked_running = []
        self.done = []
        self.results = []
        self.finished = []
        self.combinations = [ResearchCombination(1, "Breakout", "BTC/USD", "5m", {"reward_risk": 1.2})]

    def seed_combinations(self, combinations):
        self.seeded.extend(combinations)
        return len(combinations)

    def count_combinations(self):
        return self.existing_count if self.existing_count is not None else len(self.combinations)

    def fetch_next_pending(self, limit):
        return self.combinations[:limit]

    def create_batch(self, batch_size, total_combinations):
        self.batch_size = batch_size
        self.total = total_combinations
        return 10

    def mark_running(self, combination_ids):
        self.marked_running.extend(combination_ids)

    def fetch_ohlc(self, pair, timeframe, limit=720):
        return [(index, 1, 1, 1, 100 + index, 10) for index in range(220)]

    def save_result(self, combination, metrics, batch_id):
        self.results.append((combination.id, metrics["validation"]["status"], batch_id))

    def mark_done(self, combination_id, status, error=None):
        self.done.append((combination_id, status, error))

    def finish_batch(self, batch_id, status, processed, last_combination_id=None):
        self.finished.append((batch_id, status, processed, last_combination_id))


def test_progressive_research_processes_one_small_batch_and_publishes_completion():
    repo = FakeResearchRepository()
    bus = EventBus()
    events = []
    bus.subscribe(EventType.RESEARCH_COMPLETED, events.append)
    engine = ProgressiveResearchEngine(repo, bus, batch_size=100, max_runtime_minutes=20, sleep_between_batches_seconds=0)

    result = engine.process_one_batch()

    assert result.status == "COMPLETED"
    assert repo.batch_size == 100
    assert repo.marked_running == [1]
    assert repo.results == [(1, "PROGRESSIVE_BASELINE", 10)]
    assert repo.done == [(1, "DONE", None)]
    assert repo.finished == [(10, "COMPLETED", 1, 1)]
    assert events[-1].payload == {"batch_id": 10, "processed": 1}


def test_seed_combinations_streams_to_repository_in_chunks():
    repo = FakeResearchRepository(existing_count=0)
    engine = ProgressiveResearchEngine(repo, sleep_between_batches_seconds=0)

    seeded = engine.seed_combinations(["BTC/USD"], ["5m"], chunk_size=50)

    assert seeded == len(repo.seeded)
    assert seeded > 50
    assert repo.seeded[0][0] == "Breakout"
