import pytest

from project.config.settings import PlatformSettings
from project.decision_engine.service import DecisionEngine
from project.notification_engine.service import NotificationEngine
from project.position_monitor.service import PositionMonitor
from project.research_engine.repository import ResearchRepository
from project.shared.events import Event, EventBus, EventType
from project.strategy_engine.service import StrategyEngine


def test_platform_defaults_to_one_hour_timeframe(monkeypatch):
    monkeypatch.delenv("COLLECTOR_TIMEFRAMES", raising=False)
    monkeypatch.delenv("OPERATIONAL_TIMEFRAME", raising=False)

    settings = PlatformSettings()

    assert settings.collector_timeframes == ["1h"]
    assert settings.operational_timeframe == "1h"
    assert settings.signal_classes == ["A", "B"]
    assert settings.strategy_ohlc_limit == 720
    assert settings.strategy_candidate_limit == 50


def test_platform_strategy_candidate_limit_is_configurable(monkeypatch):
    monkeypatch.setenv("STRATEGY_CANDIDATE_LIMIT", "75")

    settings = PlatformSettings()

    assert settings.strategy_candidate_limit == 75


def test_platform_signal_classes_are_configurable(monkeypatch):
    monkeypatch.setenv("SIGNAL_CLASSES", "A,B")

    settings = PlatformSettings()

    assert settings.signal_classes == ["A", "B"]


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
    assert signal.timeframe == "1h"
    assert signal.net_profit_tp1_eur > 0
    assert postgres.inserted
    assert events[-1].payload["signal_id"] == 123
    assert events[-1].payload["entry_timing"] == "IMMEDIATE_ON_SIGNAL_RECEIPT"
    assert events[-1].payload["net_profit_tp1_eur"] > 0
    assert events[-1].payload["signal_class"] in {"A", "B", "C"}


def test_strategy_engine_uses_configured_ohlc_limit():
    class CapturingResearch(FakeResearchRepository):
        def __init__(self, postgres):
            super().__init__(postgres)
            self.last_limit = None

        def fetch_ohlc(self, pair, timeframe, limit=720):
            self.last_limit = limit
            return super().fetch_ohlc(pair, timeframe, limit)

    postgres = FakePostgres()
    research = CapturingResearch(postgres)
    strategy = StrategyEngine(research, DecisionEngine(postgres), ohlc_limit=360)

    strategy.evaluate()

    assert research.last_limit == 360


def test_strategy_engine_uses_configured_candidate_limit():
    class CapturingCandidateResearch(FakeResearchRepository):
        def __init__(self, postgres):
            super().__init__(postgres)
            self.last_limit = None

        def fetch_candidate_results(self, timeframe=None, limit=10):
            self.last_limit = limit
            return [self.fetch_best_result(timeframe)]

    postgres = FakePostgres()
    research = CapturingCandidateResearch(postgres)
    strategy = StrategyEngine(research, DecisionEngine(postgres), max_candidate_evaluations=75)

    strategy.evaluate()

    assert research.last_limit == 75


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
        "timeframe": "1h",
        "regime": "TREND_UP",
        "entry": 1.0,
        "stop_loss": 0.9,
        "take_profit": 1.2,
        "technical_take_profit": 1.2,
        "effective_take_profit": 1.2,
        "signal_time": "2026-01-01T00:00:00Z",
        "reference_candle_time": "2026-01-01T00:00:00Z",
        "quantity": 100.0,
        "gross_rr": 2.0,
        "net_profit_tp1_eur": 1.0,
        "net_loss_sl_eur": 1.0,
        "net_rr": 1.0,
        "stop_distance": 0.1,
        "stop_pct": 10.0,
        "atr": 0.1,
        "stop_atr_ratio": 1.0,
        "tp_atr_ratio": 2.0,
        "historical_sample_size": 50,
        "historical_win_rate": 0.6,
        "historical_expected_value_eur": 0.2,
        "probability_confidence": "MEDIUM",
        "validation_status": "PASSED",
        "signal_class": "B",
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

    assert "Class: B" in message
    assert "Quantity: 100.00000000" in message
    assert "Gross R/R: 2.00" in message
    assert "Technical TP: 1.200000" in message
    assert "Effective TP: 1.200000" in message
    assert "Probability confidence: MEDIUM" in message
    assert "Historical EV: 0.2" in message
    assert "Validation: PASSED" in message
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


def test_take_profit_is_not_moved_to_monetary_target():
    strategy = make_strategy_for_economics(capital=100, buy_fee=0.001, sell_fee=0.001, spread=0.0005)
    strategy.min_tp1_net_profit_eur = 2.0
    strategy.min_net_rr = 1.2

    raw_take_profit = 64361.422107
    economics = strategy.calculate_net_economics(64194.3, 64174.315, raw_take_profit)

    assert economics["net_profit_tp1_eur"] < 2.0


def make_prices(count=80, close=100.0, candle_range=2.0):
    return [
        (
            index,
            close,
            close + candle_range / 2,
            close - candle_range / 2,
            close,
            10,
        )
        for index in range(count)
    ]


def test_validation_rejects_stop_too_tight_for_volatility():
    strategy = make_strategy_for_economics(spread=0.001)
    prices = make_prices()
    economics = strategy.calculate_net_economics(100, 99.9, 102)
    volatility = strategy.calculate_volatility_context(prices, 100, 99.9, 102)
    probability = strategy.estimate_probability_context(prices, 100, 99.9, 102)

    validation = strategy.validate_signal_setup({"strategy": "Breakout"}, 100, 99.9, 102, economics, volatility, probability)

    assert validation["status"] == "PASSED_WITH_WARNINGS"
    assert "STOP_TOO_TIGHT_FOR_VOLATILITY" in validation["reason"]


def test_validation_rejects_target_too_far_for_atr():
    strategy = make_strategy_for_economics(spread=0)
    prices = make_prices()
    economics = strategy.calculate_net_economics(100, 98, 130)
    volatility = strategy.calculate_volatility_context(prices, 100, 98, 130)
    probability = strategy.estimate_probability_context(prices, 100, 98, 130)

    validation = strategy.validate_signal_setup({"strategy": "Breakout"}, 100, 98, 130, economics, volatility, probability)

    assert validation["status"] == "PASSED_WITH_WARNINGS"
    assert "TP_TOO_FAR_FOR_ATR" in validation["reason"]


def test_strategy_engine_allows_low_net_rr_as_lower_class_signal():
    strategy = StrategyEngine(
        FakeResearchRepository(FakePostgres()),
        DecisionEngine(FakePostgres()),
        min_net_rr=1.2,
        min_notional_eur=0.0,
    )
    economics = strategy.calculate_net_economics(100, 99, 100.5)
    validation = {"status": "PASSED_WITH_WARNINGS", "reason": "LOW_NET_RR_DIAGNOSTIC"}
    probability = {"sample_size": 10}

    signal_class = strategy.classify_signal(
        {"profit_factor": 1.12, "expectancy": 0.1},
        economics,
        validation,
        probability,
        regime_aligned=True,
    )

    assert economics["net_profit_tp1_eur"] > 0
    assert economics["net_rr"] < strategy.min_net_rr
    assert signal_class == "B"


def test_strategy_engine_respects_enabled_signal_classes():
    postgres = FakePostgres()
    bus = EventBus()
    events = []
    bus.subscribe(EventType.NEW_SIGNAL, events.append)
    decision = DecisionEngine(postgres, bus)
    research = FakeResearchRepository(postgres)
    strategy = StrategyEngine(research, decision, bus, allowed_signal_classes=["A"])

    signal = strategy.evaluate()

    assert signal is None
    assert postgres.inserted == []
    assert events == []


def test_strategy_engine_does_not_hard_reject_positive_edge_below_class_b_pf():
    class LowProfitFactorResearch(FakeResearchRepository):
        def fetch_best_result(self, timeframe=None):
            return {
                "strategy": "Breakout",
                "pair": "BTC/USD",
                "timeframe": timeframe or "1h",
                "profit_factor": 1.0816,
                "expectancy": 0.05,
                "net_profit": 5.0,
            }

    postgres = FakePostgres()
    bus = EventBus()
    events = []
    bus.subscribe(EventType.NEW_SIGNAL, events.append)
    decision = DecisionEngine(postgres, bus)
    strategy = StrategyEngine(LowProfitFactorResearch(postgres), decision, bus, allowed_signal_classes=["A", "B", "C"])

    signal = strategy.evaluate()

    assert signal is not None
    assert signal.signal_class == "C"
    assert events[-1].payload["signal_class"] == "C"


def test_strategy_engine_rejects_high_confidence_negative_historical_ev():
    class NegativeExpectedValueResearch(FakeResearchRepository):
        def fetch_best_result(self, timeframe=None):
            return {
                "strategy": "Breakout",
                "pair": "SOL/USD",
                "timeframe": timeframe or "1h",
                "profit_factor": 1.0816,
                "expectancy": 0.013755,
                "net_profit": 5.0,
            }

        def fetch_ohlc(self, pair, timeframe, limit=720):
            prices = []
            for index in range(120):
                close = 76.71
                if index % 3 == 0:
                    high = close + 0.2
                    low = close - 0.6
                else:
                    high = close + 0.2
                    low = close - 0.2
                prices.append((index, close, high, low, close, 10))
            return prices

    postgres = FakePostgres()
    bus = EventBus()
    events = []
    bus.subscribe(EventType.NEW_SIGNAL, events.append)
    decision = DecisionEngine(postgres, bus)
    strategy = StrategyEngine(NegativeExpectedValueResearch(postgres), decision, bus, allowed_signal_classes=["A", "B", "C"])

    signal = strategy.evaluate()

    assert signal is None
    assert postgres.inserted == []
    assert events == []


def test_strategy_engine_tries_next_candidate_after_negative_ev_rejection():
    class MultiCandidateResearch(FakeResearchRepository):
        def fetch_candidate_results(self, timeframe=None, limit=10):
            return [
                {
                    "strategy": "Breakout",
                    "pair": "SOL/USD",
                    "timeframe": timeframe or "1h",
                    "profit_factor": 1.20,
                    "expectancy": 0.1,
                    "net_profit": 5.0,
                },
                {
                    "strategy": "Breakout",
                    "pair": "BTC/USD",
                    "timeframe": timeframe or "1h",
                    "profit_factor": 1.18,
                    "expectancy": 0.1,
                    "net_profit": 6.0,
                },
            ]

        def fetch_ohlc(self, pair, timeframe, limit=720):
            if pair == "SOL/USD":
                return [
                    (index, 100, 100.2, 99.4 if index % 3 == 0 else 99.8, 100, 10)
                    for index in range(120)
                ]
            return [
                (index, 100, 103, 99, 102, 10)
                for index in range(20)
            ]

    postgres = FakePostgres()
    bus = EventBus()
    events = []
    bus.subscribe(EventType.NEW_SIGNAL, events.append)
    decision = DecisionEngine(postgres, bus)
    strategy = StrategyEngine(MultiCandidateResearch(postgres), decision, bus)

    signal = strategy.evaluate()

    assert signal is not None
    assert signal.pair == "BTC/USD"
    assert events[-1].payload["pair"] == "BTC/USD"


def test_probability_is_unavailable_when_historical_sample_is_insufficient():
    strategy = make_strategy_for_economics()
    context = strategy.estimate_probability_context(make_prices(count=10), 100, 98, 103)

    assert context["sample_size"] < strategy.min_probability_sample_size
    assert context["probability"] is None
    assert context["confidence"] == "LOW"


def test_conservative_same_candle_long_counts_stop_first():
    outcome = PositionMonitor.resolve_candle_outcome("LONG", high=105, low=95, take_profit=104, stop_loss=96)

    assert outcome == "STOP_LOSS"


def test_short_candle_resolution_supports_target_and_stop():
    assert PositionMonitor.resolve_candle_outcome("SHORT", high=105, low=95, take_profit=96, stop_loss=104) == "STOP_LOSS"
    assert PositionMonitor.resolve_candle_outcome("SHORT", high=103, low=95, take_profit=96, stop_loss=104) == "TARGET_HIT"


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
