"""Strategy Engine: turn validated research candidates into signal events."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from project.decision_engine.service import DecisionEngine, MarketDecision
from project.research_engine.repository import ResearchRepository
from project.shared.events import Event, EventBus, EventType
from project.shared.logging import get_module_logger


@dataclass(frozen=True)
class GeneratedSignal:
    strategy: str
    pair: str
    timeframe: str
    regime: str
    entry: float
    stop_loss: float
    take_profit: float
    signal_time: datetime
    reference_candle_time: Any
    entry_timing: str
    score: float
    probability: float
    reasons: list[str]


class StrategyEngine:
    def __init__(self, research_repository: ResearchRepository, decision_engine: DecisionEngine, event_bus: EventBus | None = None, min_profit_factor: float = 1.25) -> None:
        self.research_repository = research_repository
        self.decision_engine = decision_engine
        self.event_bus = event_bus or EventBus()
        self.min_profit_factor = min_profit_factor
        self.logger = get_module_logger("strategy")

    def evaluate(self) -> GeneratedSignal | None:
        decision = self.decision_engine.latest_decision or self.decision_engine.evaluate_market()
        best = self.research_repository.fetch_best_result()
        if not best:
            self.logger.info("STRATEGY no research candidate available")
            return None
        if best["profit_factor"] < self.min_profit_factor:
            self.logger.info("STRATEGY candidate rejected strategy=%s reason=profit_factor_below_threshold pf=%.4f", best["strategy"], best["profit_factor"])
            return None
        if best["strategy"] not in decision.enabled_strategies:
            self.logger.info("STRATEGY candidate disabled strategy=%s regime=%s enabled=%s", best["strategy"], decision.regime, decision.enabled_strategies)
            return None
        prices = self.research_repository.fetch_ohlc(best["pair"], best["timeframe"], limit=20)
        if len(prices) < 5:
            self.logger.info("STRATEGY no recent OHLC for pair=%s timeframe=%s", best["pair"], best["timeframe"])
            return None
        latest = prices[0]
        entry = float(latest[4])
        reference_candle_time = latest[0]
        signal_time = datetime.now(timezone.utc)
        ranges = [float(row[2]) - float(row[3]) for row in prices if float(row[2]) >= float(row[3])]
        avg_range = sum(ranges) / len(ranges) if ranges else entry * 0.005
        stop_loss = max(entry - avg_range, entry * 0.98)
        take_profit = entry + avg_range * 1.5
        score = min(100.0, 50.0 + best["profit_factor"] * 20.0)
        probability = min(0.75, 0.50 + (best["profit_factor"] - 1.0) / 2.0)
        signal = GeneratedSignal(
            strategy=best["strategy"],
            pair=best["pair"],
            timeframe=best["timeframe"],
            regime=decision.regime,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            signal_time=signal_time,
            reference_candle_time=reference_candle_time,
            entry_timing="IMMEDIATE_ON_SIGNAL_RECEIPT",
            score=score,
            probability=probability,
            reasons=[
                f"profit_factor={best['profit_factor']:.4f}",
                f"expectancy={best['expectancy']:.6f}",
                f"regime={decision.regime}",
            ],
        )
        duplicate_id = self.find_active_duplicate(signal)
        if duplicate_id is not None:
            self.logger.info(
                "STRATEGY duplicate skipped existing_signal_id=%s strategy=%s pair=%s timeframe=%s regime=%s",
                duplicate_id,
                signal.strategy,
                signal.pair,
                signal.timeframe,
                signal.regime,
            )
            return None
        signal_id = self.save_signal(signal)
        payload = {**signal.__dict__, "signal_id": signal_id}
        self.logger.info("NEW SIGNAL id=%s strategy=%s pair=%s timeframe=%s entry=%.6f stop=%.6f take_profit=%.6f score=%.2f", signal_id, signal.strategy, signal.pair, signal.timeframe, signal.entry, signal.stop_loss, signal.take_profit, signal.score)
        self.event_bus.publish(Event(EventType.NEW_SIGNAL, payload))
        return signal

    def find_active_duplicate(self, signal: GeneratedSignal) -> int | None:
        rows = self.research_repository.client.fetch_all(
            """SELECT id
            FROM signals.generated_signals
            WHERE strategy = %s
              AND pair = %s
              AND timeframe = %s
              AND regime = %s
              AND status IN ('NEW', 'OPEN')
            ORDER BY created_at DESC
            LIMIT 1""",
            (signal.strategy, signal.pair, signal.timeframe, signal.regime),
        )
        return int(rows[0][0]) if rows else None

    def save_signal(self, signal: GeneratedSignal) -> int:
        rows = self.research_repository.client.fetch_all(
            """INSERT INTO signals.generated_signals(
                strategy, pair, timeframe, regime, entry, stop_loss, take_profit,
                signal_time, reference_candle_time, entry_timing,
                score, probability, reasons, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            RETURNING id""",
            (
                signal.strategy,
                signal.pair,
                signal.timeframe,
                signal.regime,
                signal.entry,
                signal.stop_loss,
                signal.take_profit,
                signal.signal_time,
                signal.reference_candle_time,
                signal.entry_timing,
                signal.score,
                signal.probability,
                json.dumps(signal.reasons),
                signal.signal_time,
            ),
        )
        return int(rows[0][0])
