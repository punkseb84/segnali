from project.decision_engine.service import DecisionEngine
from project.notification_engine.service import NotificationEngine
from project.position_monitor.service import PositionMonitor
from project.research_engine.repository import ResearchRepository
from project.shared.events import Event, EventBus, EventType
from project.strategy_engine.service import StrategyEngine


class FakePostgres:
    def __init__(self):
        self.inserted = []
        self.updated = []
        self.duplicate_id = None

    def fetch_all(self, sql, params=None):
        if "SELECT id" in sql and "FROM signals.generated_signals" in sql:
            return [(self.duplicate_id,)] if self.duplicate_id else []
        if "FROM market_data.ohlc" in sql and "LIMIT 60" in sql:
            return [(100 + i, 101 + i, 99 + i, 10) for i in range(60)]
        if "FROM market_data.ohlc" in sql:
            return [(i, 1, 101 + i, 99 + i, 100 + i, 10) for i in range(20)]
        if "INSERT INTO signals.generated_signals" in sql:
            self.inserted.append(params)
            return [(123,)]
        return []

    def execute(self, sql, params=None):
        self.updated.append((sql, params))


class FakeResearchRepository(ResearchRepository):
    def __init__(self, postgres):
        self.client = postgres

    def fetch_best_result(self, timeframe=None):
        return {"strategy": "Breakout", "pair": "BTC/USD", "timeframe": timeframe or "15m", "profit_factor": 1.4, "expectancy": 0.2, "net_profit": 10.0}

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
    assert signal.entry_timing == "IMMEDIATE_ON_SIGNAL_RECEIPT"
    assert signal.timeframe == "15m"
    assert signal.net_profit_tp1_eur > 0
    assert postgres.inserted
    assert events[-1].payload["signal_id"] == 123
    assert events[-1].payload["entry_timing"] == "IMMEDIATE_ON_SIGNAL_RECEIPT"


def test_notification_engine_skips_when_disabled():
    bus = EventBus()
    notification = NotificationEngine(bus, enabled=False)
    notification.subscribe()

    bus.publish(Event(EventType.NEW_SIGNAL, {"signal_id": 1, "strategy": "Breakout", "pair": "BTC/USD", "timeframe": "15m", "regime": "TREND_UP", "entry": 1.0, "stop_loss": 0.9, "take_profit": 1.2, "signal_time": "2026-01-01T00:00:00Z", "reference_candle_time": "2026-01-01T00:00:00Z", "net_profit_tp1_eur": 1.0, "net_loss_sl_eur": 1.0, "net_rr": 1.0, "estimated_buy_fee_eur": 0.1, "estimated_sell_fee_eur": 0.1, "estimated_spread_cost_eur": 0.05, "score": 80.0, "probability": 0.6, "reasons": ["test"]}))


def test_strategy_engine_skips_active_duplicate_signal():
    postgres = FakePostgres()
    postgres.duplicate_id = 99
    bus = EventBus()
    decision = DecisionEngine(postgres, bus)
    research = FakeResearchRepository(postgres)
    strategy = StrategyEngine(research, decision, bus)

    signal = strategy.evaluate()

    assert signal is None
    assert postgres.inserted == []


def test_position_monitor_closes_target_hit_and_publishes_event():
    class PositionPostgres(FakePostgres):
        def fetch_all(self, sql, params=None):
            if "FROM signals.generated_signals" in sql:
                return [(1, "Breakout", "BTC/USD", "5m", "COMPRESSION", 100, 95, 105, 75, 0.6, "2026-01-01T00:00:00Z")]
            if "SELECT timestamp, high, low, close" in sql:
                return [("2026-01-01T00:05:00Z", 106, 99, 105)]
            return super().fetch_all(sql, params)

    postgres = PositionPostgres()
    bus = EventBus()
    events = []
    bus.subscribe(EventType.TARGET_HIT, events.append)
    monitor = PositionMonitor(postgres, bus)

    closed = monitor.monitor_open_signals()

    assert closed == 1
    assert postgres.updated[-1][1] == ("TARGET_HIT", 1)
    assert events[-1].payload["signal_id"] == 1
