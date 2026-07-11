import pytest

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
    assert events[-1].payload["net_profit_tp1_eur"] > strategy.min_tp1_net_profit_eur


def test_notification_engine_skips_when_disabled():
    bus = EventBus()
    notification = NotificationEngine(bus, enabled=False)
    notification.subscribe()

    bus.publish(Event(EventType.NEW_SIGNAL, signal_payload()))


def signal_payload():
    return {
        "signal_id": 1,
        "strategy": "Breakout",
        "pair": "BTC/USD",
        "timeframe": "15m",
        "regime": "TREND_UP",
        "entry": 1.0,
        "stop_loss": 0.9,
        "take_profit": 1.2,
        "signal_time": "2026-01-01T00:00:00Z",
        "reference_candle_time": "2026-01-01T00:00:00Z",
        "quantity": 100.0,
        "gross_rr": 2.0,
        "net_profit_tp1_eur": 1.0,
        "net_loss_sl_eur": 1.0,
        "net_rr": 1.0,
        "estimated_buy_fee_eur": 0.1,
        "estimated_sell_fee_eur": 0.1,
        "estimated_sell_fee_sl_eur": 0.1,
        "estimated_spread_cost_eur": 0.05,
        "estimated_slippage_cost_eur": 0.0,
        "score": 80.0,
        "probability": 0.6,
        "reasons": ["test"],
    }


def test_notification_report_uses_engine_economic_fields():
    message = NotificationEngine(EventBus()).format_signal(signal_payload())

    assert "Quantity: 100.00000000" in message
    assert "Gross R/R: 2.00" in message
    assert "Net profit TP1: €1.0000" in message
    assert "Net loss SL: €1.0000" in message
    assert "Net R/R: 1.00" in message
    assert "sell fee TP €0.1000" in message
    assert "sell fee SL €0.1000" in message


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


def make_strategy_for_economics(capital=100.0, buy_fee=0.001, sell_fee=0.001, spread=0.0, step=0.000001):
    return StrategyEngine(
        FakeResearchRepository(FakePostgres()),
        DecisionEngine(FakePostgres()),
        trade_notional_eur=capital,
        buy_fee_rate=buy_fee,
        sell_fee_rate=sell_fee,
        spread_rate=spread,
        quantity_step=step,
        min_net_rr=0.0,
        min_notional_eur=0.0,
    )


def test_long_tp_farther_than_sl_has_expected_gross_rr_without_costs():
    economics = make_strategy_for_economics(buy_fee=0, sell_fee=0, spread=0).calculate_net_economics(100, 99, 108)

    assert economics["gross_rr"] == pytest.approx(8.0, rel=1e-3)
    assert economics["net_rr"] == pytest.approx(8.0, rel=1e-3)


def test_fees_can_make_small_profit_net_negative():
    economics = make_strategy_for_economics(buy_fee=0.001, sell_fee=0.001, spread=0).calculate_net_economics(100, 99, 100.1)

    assert economics["gross_profit_tp1_eur"] > 0
    assert economics["net_profit_tp1_eur"] < 0


def test_profit_and_loss_scale_linearly_with_capital():
    ten = make_strategy_for_economics(capital=10, buy_fee=0, sell_fee=0, spread=0).calculate_net_economics(100, 99, 108)
    hundred = make_strategy_for_economics(capital=100, buy_fee=0, sell_fee=0, spread=0).calculate_net_economics(100, 99, 108)
    thousand = make_strategy_for_economics(capital=1000, buy_fee=0, sell_fee=0, spread=0).calculate_net_economics(100, 99, 108)

    assert hundred["gross_profit_tp1_eur"] == pytest.approx(ten["gross_profit_tp1_eur"] * 10)
    assert thousand["gross_loss_sl_eur"] == pytest.approx(hundred["gross_loss_sl_eur"] * 10)
    assert ten["gross_rr"] == pytest.approx(hundred["gross_rr"])


def test_spread_reduces_net_profit():
    no_spread = make_strategy_for_economics(buy_fee=0, sell_fee=0, spread=0).calculate_net_economics(100, 99, 108)
    with_spread = make_strategy_for_economics(buy_fee=0, sell_fee=0, spread=0.001).calculate_net_economics(100, 99, 108)

    assert with_spread["net_profit_tp1_eur"] < no_spread["net_profit_tp1_eur"]


def test_quantity_rounding_uses_binance_step():
    economics = make_strategy_for_economics(capital=100, buy_fee=0, sell_fee=0, spread=0, step=0.01).calculate_net_economics(30, 29, 35)

    assert economics["quantity"] == pytest.approx(3.33)


def test_take_profit_is_adjusted_to_minimum_net_profit_target():
    strategy = make_strategy_for_economics(capital=100, buy_fee=0.001, sell_fee=0.001, spread=0.0005)
    strategy.min_tp1_net_profit_eur = 2.0
    strategy.min_net_rr = 1.2

    take_profit = strategy.adjust_take_profit(64194.3, 64174.315, 64361.422107)
    economics = strategy.calculate_net_economics(64194.3, 64174.315, take_profit)

    assert economics["net_profit_tp1_eur"] > 2.0
    assert economics["net_rr"] >= 1.2


def test_daily_no_signal_report_is_report_not_trade_signal():
    postgres = FakePostgres()
    bus = EventBus()
    reports = []
    trade_signals = []
    bus.subscribe(EventType.REPORT_READY, reports.append)
    bus.subscribe(EventType.NEW_SIGNAL, trade_signals.append)
    strategy = StrategyEngine(
        FakeResearchRepository(postgres),
        DecisionEngine(postgres),
        bus,
        enable_daily_signal_report=True,
        daily_signal_report_hours=24,
    )

    published = strategy.publish_daily_signal_report()

    assert published is True
    assert reports
    assert trade_signals == []
    assert "Questo non è un segnale di ingresso" in reports[-1].payload["message"]
