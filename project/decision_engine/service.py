"""Decision Engine: classify market regime and enable strategy families."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from project.database.postgres import PostgresClient
from project.shared.events import Event, EventBus, EventType
from project.shared.logging import get_module_logger


@dataclass(frozen=True)
class MarketDecision:
    pair: str
    timeframe: str
    regime: str
    enabled_strategies: list[str]
    reasons: list[str]


class DecisionEngine:
    def __init__(self, postgres: PostgresClient, event_bus: EventBus | None = None, pair: str = "BTC/USD", timeframe: str = "5m") -> None:
        self.postgres = postgres
        self.event_bus = event_bus or EventBus()
        self.pair = pair
        self.timeframe = timeframe
        self.logger = get_module_logger("decision")
        self.latest_decision: MarketDecision | None = None

    def evaluate_market(self) -> MarketDecision:
        rows = self.postgres.fetch_all(
            """SELECT close, high, low, volume
            FROM market_data.ohlc
            WHERE pair = %s AND timeframe = %s
            ORDER BY timestamp DESC
            LIMIT 60""",
            (self.pair, self.timeframe),
        )
        if len(rows) < 20:
            decision = MarketDecision(self.pair, self.timeframe, "NO_TRADE", [], ["not enough OHLC data"])
            self.latest_decision = decision
            self.logger.info("DECISION regime=%s reasons=%s", decision.regime, decision.reasons)
            return decision
        closes = [float(row[0]) for row in reversed(rows)]
        highs = [float(row[1]) for row in reversed(rows)]
        lows = [float(row[2]) for row in reversed(rows)]
        recent_return = (closes[-1] - closes[-10]) / closes[-10] if closes[-10] else 0.0
        ranges = [high - low for high, low in zip(highs, lows)]
        avg_range = sum(ranges[-20:]) / 20
        avg_price = sum(closes[-20:]) / 20
        volatility = avg_range / avg_price if avg_price else 0.0
        reasons: list[str] = [f"recent_return={recent_return:.4f}", f"volatility={volatility:.4f}"]
        if volatility > 0.018:
            regime = "HIGH_VOLATILITY"
            enabled = ["Breakout", "Volatility Expansion", "Momentum"]
        elif volatility < 0.004:
            regime = "COMPRESSION"
            enabled = ["Compression Breakout", "Breakout"]
        elif recent_return > 0.003:
            regime = "TREND_UP"
            enabled = ["Breakout", "Breakout Retest", "Pullback Trend", "Trend Following", "Momentum"]
        elif recent_return < -0.003:
            regime = "TREND_DOWN"
            enabled = ["Mean Reversion", "Range Reversal"]
        else:
            regime = "RANGE"
            enabled = ["Range Reversal", "Mean Reversion", "Liquidity Sweep"]
        decision = MarketDecision(self.pair, self.timeframe, regime, enabled, reasons)
        self.latest_decision = decision
        self.logger.info("DECISION regime=%s enabled_strategies=%s reasons=%s", regime, enabled, reasons)
        self.event_bus.publish(Event(EventType.MARKET_REGIME_CHANGED, {"pair": self.pair, "timeframe": self.timeframe, "regime": regime, "enabled_strategies": enabled}))
        return decision
