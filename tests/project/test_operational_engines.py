from project.decision_engine.service import DecisionEngine
from project.notification_engine.service import NotificationEngine
from project.research_engine.repository import ResearchRepository
from project.shared.events import Event, EventBus, EventType
from project.strategy_engine.service import StrategyEngine


class FakePostgres:
    def __init__(self):
        self.inserted = []

    def fetch_all(self, sql, params=None):
        if "FROM market_data.ohlc" in sql and "LIMIT 60" in sql:
            return [(100 + i, 101 + i, 99 + i, 10) for i in range(60)]
        if "FROM market_data.ohlc" in sql:
            return [(i, 1, 101 + i, 99 + i, 100 + i, 10) for i in range(20)]
        if "INSERT INTO signals.generated_signals" in sql:
            self.inserted.append(params)
            return [(123,)]
        return []


class FakeResearchRepository(ResearchRepository):
    def __init__(self, postgres):
        self.client = postgres

    def fetch_best_result(self):
        return {"strategy": "Breakout", "pair": "BTC/USD", "timeframe": "5m", "profit_factor": 1.4, "expectancy": 0.2, "net_profit": 10.0}

    def fetch_ohlc(self, pair, timeframe, limit=720):
        return self.client.fetch_all("FROM market_data.ohlc", (pair, timeframe, limit))


def test_strategy_engine_generates_signal_event_when_candidate_enabled():
    postgres = FakePostgres()
    bus = EventBus()
    events = []
    bus.subscribe(EventType.NEW_SIGNAL, events.append)
    decision = DecisionEngine(postgres, bus)
    research = FakeResearchRepository(postgres)
    strategy = StrategyEngine(research, decision, bus)

    signal = strategy.evaluate()

    assert signal is not None
    assert signal.strategy == "Breakout"
    assert postgres.inserted
    assert events[-1].payload["signal_id"] == 123


def test_notification_engine_skips_when_disabled():
    bus = EventBus()
    notification = NotificationEngine(bus, enabled=False)
    notification.subscribe()

    bus.publish(Event(EventType.NEW_SIGNAL, {"signal_id": 1, "strategy": "Breakout", "pair": "BTC/USD", "timeframe": "5m", "regime": "TREND_UP", "entry": 1.0, "stop_loss": 0.9, "take_profit": 1.2, "score": 80.0, "probability": 0.6, "reasons": ["test"]}))
