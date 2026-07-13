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
        self.reset_count = 0

    def seed_combinations(self, combinations):
        self.seeded.extend(combinations)
        return len(combinations)

    def count_combinations(self):
        return self.existing_count if self.existing_count is not None else len(self.combinations)

    def existing_timeframes(self, timeframes):
        return set()

    def fetch_next_pending(self, limit, priority_timeframe=None):
        self.priority_timeframe = priority_timeframe
        return self.combinations[:limit]

    def create_batch(self, batch_size, total_combinations):
        self.batch_size = batch_size
        self.total = total_combinations
        return 10

    def mark_running(self, combination_ids):
        self.marked_running.extend(combination_ids)

    def fetch_ohlc(self, pair, timeframe, limit=720):
        return [(index, 100 + index, 102 + index, 99 + index, 101 + index, 10) for index in range(240)]

    def reset_stale_results(self, required_validation_status="LIVE_ALIGNED_BACKTEST"):
        self.required_validation_status = required_validation_status
        return self.reset_count

    def fetch_progress_counts(self):
        return {"DONE": len([item for item in self.done if item[1] == "DONE"]), "PENDING": 0, "FAILED_RETRYABLE": 0, "RUNNING": 0}

    def fetch_best_result(self, timeframe=None):
        return {"strategy": "Breakout", "pair": "BTC/USD", "timeframe": "5m", "profit_factor": 1.2, "expectancy": 0.1, "net_profit": 1.0}

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
    assert repo.required_validation_status == "LIVE_ALIGNED_BACKTEST"
    assert repo.batch_size == 100
    assert repo.marked_running == [1]
    assert repo.results == [(1, "LIVE_ALIGNED_BACKTEST", 10)]
    assert repo.done == [(1, "DONE", None)]
    assert repo.finished == [(10, "COMPLETED", 1, 1)]
    assert events[-1].payload == {"batch_id": 10, "processed": 1}



def test_progressive_research_uses_live_aligned_trade_simulation_after_costs():
    repo = FakeResearchRepository()
    engine = ProgressiveResearchEngine(
        repo,
        sleep_between_batches_seconds=0,
        trade_notional_eur=100.0,
        buy_fee_rate=0.001,
        sell_fee_rate=0.001,
        spread_rate=0.0005,
        probability_horizon_candles=8,
    )

    metrics = engine.evaluate_combination(repo.combinations[0])

    assert metrics["validation"]["status"] == "LIVE_ALIGNED_BACKTEST"
    assert metrics["validation"]["trades"] > 0
    assert metrics["validation"]["trade_notional_eur"] == 100.0
    assert metrics["profit_factor"] >= 0.0
    assert "net_profit" in metrics
    assert "expectancy" in metrics

def test_progressive_research_passes_priority_timeframe_to_repository():
    repo = FakeResearchRepository()
    engine = ProgressiveResearchEngine(repo, batch_size=100, sleep_between_batches_seconds=0, priority_timeframe="15m")

    engine.process_one_batch()

    assert repo.priority_timeframe == "15m"


def test_seed_combinations_streams_to_repository_in_chunks():
    repo = FakeResearchRepository(existing_count=0)
    engine = ProgressiveResearchEngine(repo, sleep_between_batches_seconds=0)

    seeded = engine.seed_combinations(["BTC/USD"], ["5m"], chunk_size=50)

    assert seeded == len(repo.seeded)
    assert seeded > 50
    assert repo.seeded[0][0] == "Breakout"


def test_seed_combinations_adds_new_operational_timeframe_when_existing_data_is_for_other_timeframes():
    class TimeframeAwareRepository(FakeResearchRepository):
        def existing_timeframes(self, timeframes):
            return {"15m"}

    repo = TimeframeAwareRepository(existing_count=100)
    engine = ProgressiveResearchEngine(repo, sleep_between_batches_seconds=0)

    seeded = engine.seed_combinations(["BTC/USD"], ["1h"], chunk_size=50)

    assert seeded == len(repo.seeded)
    assert seeded > 0
    assert {item[2] for item in repo.seeded} == {"1h"}

class SqlCaptureClient:
    def __init__(self):
        self.sql = ""
        self.params = None

    def fetch_all(self, sql, params=None):
        self.sql = sql
        self.params = params
        return []


def test_best_result_query_excludes_bootstrap_records():
    from project.research_engine.repository import ResearchRepository

    client = SqlCaptureClient()
    repository = ResearchRepository(client)

    assert repository.fetch_best_result() is None
    assert "BOOTSTRAP_TEST" in client.sql
    assert "validation->>'status'" in client.sql


def test_candidate_results_deduplicate_strategy_pair_timeframe():
    from project.research_engine.repository import ResearchRepository

    client = SqlCaptureClient()
    repository = ResearchRepository(client)

    assert repository.fetch_candidate_results(timeframe="1h", limit=10) == []
    assert "DISTINCT ON (strategy, pair, timeframe)" in client.sql
    assert "WITH best_per_market" in client.sql
    assert "LIVE_ALIGNED_BACKTEST" in client.sql
    assert "profit_factor > 1.0 OR expectancy > 0" in client.sql
    assert client.params == ("1h", 10)


def test_stale_result_reset_marks_non_live_aligned_done_combinations_pending():
    from project.research_engine.repository import ResearchRepository

    class ResetCaptureClient(SqlCaptureClient):
        def fetch_all(self, sql, params=None):
            self.sql = sql
            self.params = params
            return [(1,), (2,)]

    client = ResetCaptureClient()
    repository = ResearchRepository(client)

    assert repository.reset_stale_results() == 2
    assert "status = 'PENDING'" in client.sql
    assert "LIVE_ALIGNED_BACKTEST" in client.params


def test_pending_query_prioritizes_operational_timeframe():
    from project.research_engine.repository import ResearchRepository

    client = SqlCaptureClient()
    repository = ResearchRepository(client)

    assert repository.fetch_next_pending(100, priority_timeframe="15m") == []
    assert "CASE WHEN timeframe = %s THEN 0 ELSE 1 END" in client.sql
    assert client.params == ("15m", 100)
