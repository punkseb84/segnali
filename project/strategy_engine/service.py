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
    gross_profit_tp1_eur: float
    estimated_buy_fee_eur: float
    estimated_sell_fee_eur: float
    estimated_spread_cost_eur: float
    net_profit_tp1_eur: float
    net_loss_sl_eur: float
    net_rr: float
    score: float
    probability: float
    reasons: list[str]


class StrategyEngine:
    def __init__(
        self,
        research_repository: ResearchRepository,
        decision_engine: DecisionEngine,
        event_bus: EventBus | None = None,
        min_profit_factor: float = 1.10,
        operational_timeframe: str = "15m",
        trade_notional_eur: float = 100.0,
        buy_fee_rate: float = 0.001,
        sell_fee_rate: float = 0.001,
        spread_rate: float = 0.0005,
        min_tp1_net_profit_eur: float = 0.01,
        signal_cooldown_minutes: int = 45,
    ) -> None:
        self.research_repository = research_repository
        self.decision_engine = decision_engine
        self.event_bus = event_bus or EventBus()
        self.min_profit_factor = min_profit_factor
        self.operational_timeframe = operational_timeframe
        self.trade_notional_eur = trade_notional_eur
        self.buy_fee_rate = buy_fee_rate
        self.sell_fee_rate = sell_fee_rate
        self.spread_rate = spread_rate
        self.min_tp1_net_profit_eur = min_tp1_net_profit_eur
        self.signal_cooldown_minutes = signal_cooldown_minutes
        self.logger = get_module_logger("strategy")

    def evaluate(self) -> GeneratedSignal | None:
        decision = self.decision_engine.latest_decision or self.decision_engine.evaluate_market()
        best = self.research_repository.fetch_best_result(timeframe=self.operational_timeframe)
        if not best:
            self.logger.info("STRATEGY no research candidate available timeframe=%s", self.operational_timeframe)
            return None
        if best["timeframe"] != self.operational_timeframe:
            self.logger.info("STRATEGY candidate rejected strategy=%s reason=non_operational_timeframe timeframe=%s required=%s", best["strategy"], best["timeframe"], self.operational_timeframe)
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
        raw_take_profit = entry + avg_range * 1.5
        take_profit = self.ensure_positive_tp1(entry, raw_take_profit)
        economics = self.calculate_net_economics(entry, stop_loss, take_profit)
        if economics["net_profit_tp1_eur"] <= 0:
            self.logger.info(
                "STRATEGY candidate rejected strategy=%s reason=tp1_net_profit_not_positive net_profit=%.6f",
                best["strategy"],
                economics["net_profit_tp1_eur"],
            )
            return None
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
            gross_profit_tp1_eur=economics["gross_profit_tp1_eur"],
            estimated_buy_fee_eur=economics["estimated_buy_fee_eur"],
            estimated_sell_fee_eur=economics["estimated_sell_fee_eur"],
            estimated_spread_cost_eur=economics["estimated_spread_cost_eur"],
            net_profit_tp1_eur=economics["net_profit_tp1_eur"],
            net_loss_sl_eur=economics["net_loss_sl_eur"],
            net_rr=economics["net_rr"],
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
                "STRATEGY duplicate skipped existing_signal_id=%s cooldown_minutes=%s strategy=%s pair=%s timeframe=%s regime=%s",
                duplicate_id,
                self.signal_cooldown_minutes,
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
              AND created_at >= NOW() - (%s * INTERVAL '1 minute')
            ORDER BY created_at DESC
            LIMIT 1""",
            (signal.strategy, signal.pair, signal.timeframe, signal.regime, self.signal_cooldown_minutes),
        )
        return int(rows[0][0]) if rows else None

    def ensure_positive_tp1(self, entry: float, raw_take_profit: float) -> float:
        half_spread = self.spread_rate / 2.0
        effective_buy_price = entry * (1.0 + half_spread + self.buy_fee_rate)
        sell_multiplier = 1.0 - half_spread - self.sell_fee_rate
        required_net_return = self.min_tp1_net_profit_eur / self.trade_notional_eur
        minimum_take_profit = effective_buy_price * (1.0 + required_net_return) / sell_multiplier
        return max(raw_take_profit, minimum_take_profit)

    def calculate_net_economics(self, entry: float, stop_loss: float, take_profit: float) -> dict[str, float]:
        half_spread = self.spread_rate / 2.0
        buy_fee = self.trade_notional_eur * self.buy_fee_rate
        gross_profit = self.trade_notional_eur * ((take_profit / entry) - 1.0)
        gross_loss = self.trade_notional_eur * max(0.0, 1.0 - (stop_loss / entry))
        sell_notional_tp = self.trade_notional_eur * (take_profit / entry)
        sell_fee = sell_notional_tp * self.sell_fee_rate
        spread_cost = self.trade_notional_eur * self.spread_rate
        net_profit = gross_profit - buy_fee - sell_fee - spread_cost
        stop_sell_notional = self.trade_notional_eur * (stop_loss / entry)
        stop_sell_fee = stop_sell_notional * self.sell_fee_rate
        net_loss = gross_loss + buy_fee + stop_sell_fee + spread_cost
        return {
            "gross_profit_tp1_eur": gross_profit,
            "estimated_buy_fee_eur": buy_fee,
            "estimated_sell_fee_eur": sell_fee,
            "estimated_spread_cost_eur": spread_cost,
            "net_profit_tp1_eur": net_profit,
            "net_loss_sl_eur": net_loss,
            "net_rr": net_profit / net_loss if net_loss > 0 else 0.0,
        }

    def save_signal(self, signal: GeneratedSignal) -> int:
        rows = self.research_repository.client.fetch_all(
            """INSERT INTO signals.generated_signals(
                strategy, pair, timeframe, regime, entry, stop_loss, take_profit,
                signal_time, reference_candle_time, entry_timing,
                gross_profit_tp1_eur, estimated_buy_fee_eur, estimated_sell_fee_eur,
                estimated_spread_cost_eur, net_profit_tp1_eur, net_loss_sl_eur, net_rr,
                score, probability, reasons, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
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
                signal.gross_profit_tp1_eur,
                signal.estimated_buy_fee_eur,
                signal.estimated_sell_fee_eur,
                signal.estimated_spread_cost_eur,
                signal.net_profit_tp1_eur,
                signal.net_loss_sl_eur,
                signal.net_rr,
                signal.score,
                signal.probability,
                json.dumps(signal.reasons),
                signal.signal_time,
            ),
        )
        return int(rows[0][0])
