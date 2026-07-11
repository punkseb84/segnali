"""Strategy Engine: turn validated research candidates into signal events."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from project.decision_engine.service import DecisionEngine
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
    quantity: float
    entry_notional_eur: float
    tp_notional_eur: float
    sl_notional_eur: float
    gross_profit_tp1_eur: float
    gross_loss_sl_eur: float
    estimated_buy_fee_eur: float
    estimated_sell_fee_eur: float
    estimated_sell_fee_sl_eur: float
    estimated_spread_cost_eur: float
    estimated_slippage_cost_eur: float
    net_profit_tp1_eur: float
    net_loss_sl_eur: float
    gross_rr: float
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
        min_tp1_net_profit_eur: float = 2.0,
        signal_cooldown_minutes: int = 45,
        min_net_rr: float = 1.20,
        slippage_rate: float = 0.0,
        quantity_step: float = 0.000001,
        min_qty: float = 0.0,
        min_notional_eur: float = 10.0,
        max_take_profit_distance_pct: float = 0.03,
        enable_daily_signal_report: bool = True,
        daily_signal_report_hours: int = 24,
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
        self.min_net_rr = min_net_rr
        self.slippage_rate = slippage_rate
        self.quantity_step = quantity_step
        self.min_qty = min_qty
        self.min_notional_eur = min_notional_eur
        self.max_take_profit_distance_pct = max_take_profit_distance_pct
        self.enable_daily_signal_report = enable_daily_signal_report
        self.daily_signal_report_hours = daily_signal_report_hours
        self._last_no_trade_report_at: datetime | None = None
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
            self.logger.info("STRATEGY candidate rejected strategy=%s reason=profit_factor_below_threshold pf=%.4f threshold=%.4f", best["strategy"], best["profit_factor"], self.min_profit_factor)
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
        take_profit = self.adjust_take_profit(entry, stop_loss, raw_take_profit)
        economics = self.calculate_net_economics(entry, stop_loss, take_profit)
        if not economics["tradable"]:
            self.logger.info(
                "STRATEGY candidate rejected strategy=%s reason=not_tradable quantity=%.8f entry_notional=%.6f min_qty=%.8f min_notional=%.6f",
                best["strategy"],
                economics["quantity"],
                economics["entry_notional_eur"],
                self.min_qty,
                self.min_notional_eur,
            )
            return None
        if economics["net_profit_tp1_eur"] <= self.min_tp1_net_profit_eur:
            self.logger.info(
                "STRATEGY candidate rejected strategy=%s reason=tp1_net_profit_not_positive net_profit=%.6f min_net_profit=%.6f",
                best["strategy"], economics["net_profit_tp1_eur"], self.min_tp1_net_profit_eur,
            )
            return None
        if economics["net_rr"] < self.min_net_rr:
            self.logger.info(
                "STRATEGY candidate rejected strategy=%s reason=net_rr_below_threshold net_rr=%.4f min_net_rr=%.4f net_profit=%.6f net_loss=%.6f gross_rr=%.4f",
                best["strategy"], economics["net_rr"], self.min_net_rr, economics["net_profit_tp1_eur"], economics["net_loss_sl_eur"], economics["gross_rr"],
            )
            return None
        if take_profit > entry * (1.0 + self.max_take_profit_distance_pct):
            self.logger.info(
                "STRATEGY candidate rejected strategy=%s reason=take_profit_too_far take_profit=%.6f max_take_profit=%.6f min_net_profit=%.6f net_rr=%.4f",
                best["strategy"],
                take_profit,
                entry * (1.0 + self.max_take_profit_distance_pct),
                economics["net_profit_tp1_eur"],
                economics["net_rr"],
            )
            return None
        score = min(100.0, 50.0 + best["profit_factor"] * 20.0)
        probability = min(0.75, 0.50 + (best["profit_factor"] - 1.0) / 2.0)
        signal = GeneratedSignal(
            strategy=best["strategy"], pair=best["pair"], timeframe=best["timeframe"], regime=decision.regime,
            entry=entry, stop_loss=stop_loss, take_profit=take_profit, signal_time=signal_time,
            reference_candle_time=reference_candle_time, entry_timing="IMMEDIATE_ON_SIGNAL_RECEIPT",
            quantity=economics["quantity"], entry_notional_eur=economics["entry_notional_eur"],
            tp_notional_eur=economics["tp_notional_eur"], sl_notional_eur=economics["sl_notional_eur"],
            gross_profit_tp1_eur=economics["gross_profit_tp1_eur"], gross_loss_sl_eur=economics["gross_loss_sl_eur"],
            estimated_buy_fee_eur=economics["estimated_buy_fee_eur"], estimated_sell_fee_eur=economics["estimated_sell_fee_eur"],
            estimated_sell_fee_sl_eur=economics["estimated_sell_fee_sl_eur"], estimated_spread_cost_eur=economics["estimated_spread_cost_eur"],
            estimated_slippage_cost_eur=economics["estimated_slippage_cost_eur"], net_profit_tp1_eur=economics["net_profit_tp1_eur"],
            net_loss_sl_eur=economics["net_loss_sl_eur"], gross_rr=economics["gross_rr"], net_rr=economics["net_rr"],
            score=score, probability=probability,
            reasons=[f"profit_factor={best['profit_factor']:.4f}", f"expectancy={best['expectancy']:.6f}", f"regime={decision.regime}"],
        )
        duplicate_id = self.find_active_duplicate(signal)
        if duplicate_id is not None:
            self.logger.info("STRATEGY duplicate skipped existing_signal_id=%s cooldown_minutes=%s strategy=%s pair=%s timeframe=%s regime=%s", duplicate_id, self.signal_cooldown_minutes, signal.strategy, signal.pair, signal.timeframe, signal.regime)
            return None
        signal_id = self.save_signal(signal)
        self.log_economic_debug(signal_id, signal)
        payload = {**signal.__dict__, "signal_id": signal_id}
        self.logger.info("NEW SIGNAL id=%s strategy=%s pair=%s timeframe=%s entry=%.6f stop=%.6f take_profit=%.6f score=%.2f", signal_id, signal.strategy, signal.pair, signal.timeframe, signal.entry, signal.stop_loss, signal.take_profit, signal.score)
        self.event_bus.publish(Event(EventType.NEW_SIGNAL, payload))
        return signal

    def adjust_take_profit(self, entry: float, stop_loss: float, raw_take_profit: float) -> float:
        """Raise TP only when required to make the trade economically meaningful.

        The strategy can propose a technical TP, but the live signal must clear net
        profit and net R/R floors after Binance costs. This keeps 100 EUR signals
        from being sent with a few cents of expected profit.
        """
        max_take_profit = entry * (1.0 + self.max_take_profit_distance_pct)
        take_profit = max(raw_take_profit, entry)
        economics = self.calculate_net_economics(entry, stop_loss, take_profit)
        if economics["net_profit_tp1_eur"] > self.min_tp1_net_profit_eur and economics["net_rr"] >= self.min_net_rr:
            return take_profit

        low = take_profit
        high = max(low, max_take_profit)
        for _ in range(32):
            mid = (low + high) / 2.0
            economics = self.calculate_net_economics(entry, stop_loss, mid)
            if economics["net_profit_tp1_eur"] > self.min_tp1_net_profit_eur and economics["net_rr"] >= self.min_net_rr:
                high = mid
            else:
                low = mid
        adjusted = high
        if adjusted > raw_take_profit:
            adjusted_economics = self.calculate_net_economics(entry, stop_loss, adjusted)
            self.logger.info(
                "STRATEGY take_profit_adjusted raw=%.6f adjusted=%.6f target_net_profit=%.6f target_net_rr=%.4f expected_net_profit=%.6f expected_net_rr=%.4f",
                raw_take_profit,
                adjusted,
                self.min_tp1_net_profit_eur,
                self.min_net_rr,
                adjusted_economics["net_profit_tp1_eur"],
                adjusted_economics["net_rr"],
            )
        return adjusted

    def publish_daily_signal_report(self) -> bool:
        if not self.enable_daily_signal_report:
            return False
        now = datetime.now(timezone.utc)
        if self._last_no_trade_report_at is not None:
            elapsed_hours = (now - self._last_no_trade_report_at).total_seconds() / 3600.0
            if elapsed_hours < self.daily_signal_report_hours:
                return False
        recent = self.research_repository.client.fetch_all(
            """SELECT COUNT(*)
            FROM signals.generated_signals
            WHERE created_at >= NOW() - (%s * INTERVAL '1 hour')""",
            (self.daily_signal_report_hours,),
        )
        recent_count = int(recent[0][0] or 0) if recent else 0
        if recent_count > 0:
            return False
        message = (
            "📊 Daily signal report\n"
            f"Nessun segnale operativo nelle ultime {self.daily_signal_report_hours}h.\n"
            f"Motivo: nessun setup ha superato i filtri netti minimi "
            f"(MIN_TP1_NET_PROFIT_EUR=€{self.min_tp1_net_profit_eur:.2f}, MIN_NET_RR={self.min_net_rr:.2f}).\n"
            "Questo non è un segnale di ingresso: evita operazioni forzate a profitto atteso insufficiente."
        )
        self.event_bus.publish(Event(EventType.REPORT_READY, {"message": message}))
        self._last_no_trade_report_at = now
        self.logger.info("STRATEGY daily no-signal report published hours=%s", self.daily_signal_report_hours)
        return True

    def find_active_duplicate(self, signal: GeneratedSignal) -> int | None:
        rows = self.research_repository.client.fetch_all(
            """SELECT id
            FROM signals.generated_signals
            WHERE strategy = %s AND pair = %s AND timeframe = %s AND regime = %s
              AND status IN ('NEW', 'OPEN')
              AND created_at >= NOW() - (%s * INTERVAL '1 minute')
            ORDER BY created_at DESC
            LIMIT 1""",
            (signal.strategy, signal.pair, signal.timeframe, signal.regime, self.signal_cooldown_minutes),
        )
        return int(rows[0][0]) if rows else None

    def round_quantity(self, quantity: float) -> float:
        if self.quantity_step <= 0:
            return quantity
        return math.floor(quantity / self.quantity_step) * self.quantity_step

    def calculate_net_economics(self, entry: float, stop_loss: float, take_profit: float) -> dict[str, float | bool]:
        half_spread = self.spread_rate / 2.0
        effective_entry_price = entry * (1.0 + half_spread + self.slippage_rate)
        quantity = self.round_quantity(self.trade_notional_eur / effective_entry_price) if effective_entry_price > 0 else 0.0
        entry_notional = quantity * entry
        tp_notional = quantity * take_profit
        sl_notional = quantity * stop_loss
        gross_profit = max(tp_notional - entry_notional, 0.0)
        gross_loss = max(entry_notional - sl_notional, 0.0)
        buy_fee = entry_notional * self.buy_fee_rate
        sell_fee_tp = tp_notional * self.sell_fee_rate
        sell_fee_sl = sl_notional * self.sell_fee_rate
        spread_cost = quantity * ((entry * half_spread) + (take_profit * half_spread))
        spread_cost_sl = quantity * ((entry * half_spread) + (stop_loss * half_spread))
        slippage_cost = quantity * ((entry * self.slippage_rate) + (take_profit * self.slippage_rate))
        slippage_cost_sl = quantity * ((entry * self.slippage_rate) + (stop_loss * self.slippage_rate))
        net_profit = gross_profit - buy_fee - sell_fee_tp - spread_cost - slippage_cost
        net_loss = gross_loss + buy_fee + sell_fee_sl + spread_cost_sl + slippage_cost_sl
        gross_rr = gross_profit / gross_loss if gross_loss > 0 else 0.0
        net_rr = net_profit / net_loss if net_loss > 0 else 0.0
        tradable = quantity >= self.min_qty and entry_notional >= self.min_notional_eur
        return {
            "tradable": tradable, "quantity": quantity, "entry_notional_eur": entry_notional, "tp_notional_eur": tp_notional,
            "sl_notional_eur": sl_notional, "gross_profit_tp1_eur": gross_profit, "gross_loss_sl_eur": gross_loss,
            "estimated_buy_fee_eur": buy_fee, "estimated_sell_fee_eur": sell_fee_tp, "estimated_sell_fee_sl_eur": sell_fee_sl,
            "estimated_spread_cost_eur": spread_cost, "estimated_slippage_cost_eur": slippage_cost,
            "net_profit_tp1_eur": net_profit, "net_loss_sl_eur": net_loss, "gross_rr": gross_rr, "net_rr": net_rr,
        }

    def log_economic_debug(self, signal_id: int, signal: GeneratedSignal) -> None:
        self.logger.info(
            "ECONOMIC_CALC_DEBUG signal_id=%s pair=%s entry=%.6f stop=%.6f take_profit=%.6f capital_eur=%.6f capital_quote=%.6f quantity=%.8f entry_notional=%.6f tp_notional=%.6f sl_notional=%.6f gross_profit_tp=%.6f gross_loss_sl=%.6f buy_fee=%.6f sell_fee_tp=%.6f sell_fee_sl=%.6f spread_cost=%.6f slippage_cost=%.6f net_profit_tp=%.6f net_loss_sl=%.6f gross_rr=%.4f net_rr=%.4f",
            signal_id, signal.pair, signal.entry, signal.stop_loss, signal.take_profit, self.trade_notional_eur, self.trade_notional_eur,
            signal.quantity, signal.entry_notional_eur, signal.tp_notional_eur, signal.sl_notional_eur,
            signal.gross_profit_tp1_eur, signal.gross_loss_sl_eur, signal.estimated_buy_fee_eur,
            signal.estimated_sell_fee_eur, signal.estimated_sell_fee_sl_eur, signal.estimated_spread_cost_eur,
            signal.estimated_slippage_cost_eur, signal.net_profit_tp1_eur, signal.net_loss_sl_eur, signal.gross_rr, signal.net_rr,
        )

    def save_signal(self, signal: GeneratedSignal) -> int:
        rows = self.research_repository.client.fetch_all(
            """INSERT INTO signals.generated_signals(
                strategy, pair, timeframe, regime, entry, stop_loss, take_profit,
                signal_time, reference_candle_time, entry_timing,
                quantity, entry_notional_eur, tp_notional_eur, sl_notional_eur,
                gross_profit_tp1_eur, gross_loss_sl_eur, estimated_buy_fee_eur, estimated_sell_fee_eur,
                estimated_sell_fee_sl_eur, estimated_spread_cost_eur, estimated_slippage_cost_eur,
                net_profit_tp1_eur, net_loss_sl_eur, gross_rr, net_rr,
                score, probability, reasons, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            RETURNING id""",
            (
                signal.strategy, signal.pair, signal.timeframe, signal.regime, signal.entry, signal.stop_loss, signal.take_profit,
                signal.signal_time, signal.reference_candle_time, signal.entry_timing, signal.quantity, signal.entry_notional_eur,
                signal.tp_notional_eur, signal.sl_notional_eur, signal.gross_profit_tp1_eur, signal.gross_loss_sl_eur,
                signal.estimated_buy_fee_eur, signal.estimated_sell_fee_eur, signal.estimated_sell_fee_sl_eur,
                signal.estimated_spread_cost_eur, signal.estimated_slippage_cost_eur, signal.net_profit_tp1_eur,
                signal.net_loss_sl_eur, signal.gross_rr, signal.net_rr, signal.score, signal.probability,
                json.dumps(signal.reasons), signal.signal_time,
            ),
        )
        return int(rows[0][0])
