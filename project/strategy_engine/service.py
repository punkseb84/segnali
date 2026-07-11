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
    technical_take_profit: float
    effective_take_profit: float
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
    stop_distance: float
    stop_pct: float
    atr: float
    stop_atr_ratio: float
    tp_atr_ratio: float
    historical_sample_size: int
    historical_win_rate: float | None
    historical_mfe_percentile: float
    historical_mae_percentile: float
    probability_confidence: str
    validation_status: str
    validation_reason: str
    score: float
    probability: float | None
    reasons: list[str]


class StrategyEngine:
    def __init__(
        self,
        research_repository: ResearchRepository,
        decision_engine: DecisionEngine,
        event_bus: EventBus | None = None,
        min_profit_factor: float = 1.10,
        operational_timeframe: str = "1h",
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
        min_stop_atr_ratio: float = 0.75,
        min_stop_spread_multiple: float = 2.0,
        max_tp_atr_multiple: float = 8.0,
        historical_mfe_percentile: float = 90.0,
        min_probability_sample_size: int = 30,
        probability_horizon_candles: int = 8,
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
        self.min_stop_atr_ratio = min_stop_atr_ratio
        self.min_stop_spread_multiple = min_stop_spread_multiple
        self.max_tp_atr_multiple = max_tp_atr_multiple
        self.historical_mfe_percentile = historical_mfe_percentile
        self.min_probability_sample_size = min_probability_sample_size
        self.probability_horizon_candles = probability_horizon_candles
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
        prices = self.research_repository.fetch_ohlc(best["pair"], best["timeframe"], limit=120)
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
        technical_take_profit = entry + avg_range * 1.5
        take_profit = technical_take_profit
        economics = self.calculate_net_economics(entry, stop_loss, take_profit)
        volatility = self.calculate_volatility_context(prices, entry, stop_loss, take_profit)
        probability_context = self.estimate_probability_context(prices, entry, stop_loss, take_profit)
        validation = self.validate_signal_setup(best, entry, stop_loss, take_profit, economics, volatility, probability_context)
        if validation["status"] == "REJECTED":
            self.logger.info(
                "SIGNAL_REJECTED reason=%s strategy=%s pair=%s timeframe=%s entry=%.6f stop=%.6f technical_tp=%.6f effective_tp=%.6f stop_distance=%.6f stop_pct=%.4f atr=%.6f stop_atr_ratio=%.4f tp_atr_ratio=%.4f gross_rr=%.4f net_rr=%.4f sample=%s win_rate=%s mfe_percentile=%.6f mae_percentile=%.6f",
                validation["reason"],
                best["strategy"],
                best["pair"],
                best["timeframe"],
                entry,
                stop_loss,
                technical_take_profit,
                take_profit,
                volatility["stop_distance"],
                volatility["stop_pct"],
                volatility["atr"],
                volatility["stop_atr_ratio"],
                volatility["tp_atr_ratio"],
                economics["gross_rr"],
                economics["net_rr"],
                probability_context["sample_size"],
                probability_context["win_rate"],
                probability_context["mfe_percentile"],
                probability_context["mae_percentile"],
            )
            return None
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
        score = min(100.0, 50.0 + best["profit_factor"] * 20.0)
        probability = probability_context["probability"]
        signal = GeneratedSignal(
            strategy=best["strategy"], pair=best["pair"], timeframe=best["timeframe"], regime=decision.regime,
            entry=entry, stop_loss=stop_loss, take_profit=take_profit, technical_take_profit=technical_take_profit,
            effective_take_profit=take_profit, signal_time=signal_time,
            reference_candle_time=reference_candle_time, entry_timing="IMMEDIATE_ON_SIGNAL_RECEIPT",
            quantity=economics["quantity"], entry_notional_eur=economics["entry_notional_eur"],
            tp_notional_eur=economics["tp_notional_eur"], sl_notional_eur=economics["sl_notional_eur"],
            gross_profit_tp1_eur=economics["gross_profit_tp1_eur"], gross_loss_sl_eur=economics["gross_loss_sl_eur"],
            estimated_buy_fee_eur=economics["estimated_buy_fee_eur"], estimated_sell_fee_eur=economics["estimated_sell_fee_eur"],
            estimated_sell_fee_sl_eur=economics["estimated_sell_fee_sl_eur"], estimated_spread_cost_eur=economics["estimated_spread_cost_eur"],
            estimated_slippage_cost_eur=economics["estimated_slippage_cost_eur"], net_profit_tp1_eur=economics["net_profit_tp1_eur"],
            net_loss_sl_eur=economics["net_loss_sl_eur"], gross_rr=economics["gross_rr"], net_rr=economics["net_rr"],
            stop_distance=volatility["stop_distance"], stop_pct=volatility["stop_pct"], atr=volatility["atr"],
            stop_atr_ratio=volatility["stop_atr_ratio"], tp_atr_ratio=volatility["tp_atr_ratio"],
            historical_sample_size=probability_context["sample_size"], historical_win_rate=probability_context["win_rate"],
            historical_mfe_percentile=probability_context["mfe_percentile"], historical_mae_percentile=probability_context["mae_percentile"],
            probability_confidence=probability_context["confidence"], validation_status=validation["status"], validation_reason=validation["reason"],
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

    def calculate_volatility_context(self, prices: list[tuple[Any, ...]], entry: float, stop_loss: float, take_profit: float) -> dict[str, float]:
        atr = self.calculate_atr(prices)
        ranges = [float(row[2]) - float(row[3]) for row in prices if float(row[2]) >= float(row[3])]
        avg_range = sum(ranges) / len(ranges) if ranges else atr
        stop_distance = abs(entry - stop_loss)
        tp_distance = abs(take_profit - entry)
        return {
            "atr": atr,
            "avg_range": avg_range,
            "stop_distance": stop_distance,
            "tp_distance": tp_distance,
            "stop_pct": (stop_distance / entry * 100.0) if entry else 0.0,
            "stop_atr_ratio": stop_distance / atr if atr else 0.0,
            "tp_atr_ratio": tp_distance / atr if atr else 0.0,
            "spread_price": entry * self.spread_rate,
        }

    @staticmethod
    def calculate_atr(prices: list[tuple[Any, ...]], period: int = 14) -> float:
        chronological = list(reversed(prices))
        if len(chronological) < 2:
            return 0.0
        true_ranges: list[float] = []
        previous_close = float(chronological[0][4])
        for row in chronological[1:]:
            high = float(row[2])
            low = float(row[3])
            true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
            previous_close = float(row[4])
        window = true_ranges[-period:]
        return sum(window) / len(window) if window else 0.0

    def estimate_probability_context(self, prices: list[tuple[Any, ...]], entry: float, stop_loss: float, take_profit: float) -> dict[str, Any]:
        samples = self.build_historical_outcomes(prices, stop_distance=abs(entry - stop_loss), tp_distance=abs(take_profit - entry))
        sample_size = len(samples)
        wins = [item for item in samples if item["outcome"] == "TARGET_HIT"]
        mfes = [item["mfe"] for item in samples]
        maes = [item["mae"] for item in samples]
        win_rate = len(wins) / sample_size if sample_size else None
        confidence = "LOW"
        if sample_size >= self.min_probability_sample_size * 3:
            confidence = "HIGH"
        elif sample_size >= self.min_probability_sample_size:
            confidence = "MEDIUM"
        probability = win_rate if sample_size >= self.min_probability_sample_size else None
        return {
            "sample_size": sample_size,
            "win_rate": win_rate,
            "probability": probability,
            "confidence": confidence,
            "mfe_percentile": self.percentile(mfes, self.historical_mfe_percentile),
            "mae_percentile": self.percentile(maes, self.historical_mfe_percentile),
        }

    def build_historical_outcomes(self, prices: list[tuple[Any, ...]], stop_distance: float, tp_distance: float) -> list[dict[str, float | str]]:
        chronological = list(reversed(prices))
        outcomes: list[dict[str, float | str]] = []
        horizon = max(1, self.probability_horizon_candles)
        for index in range(0, max(0, len(chronological) - horizon - 1)):
            sample_entry = float(chronological[index][4])
            sample_stop = sample_entry - stop_distance
            sample_tp = sample_entry + tp_distance
            future = chronological[index + 1 : index + 1 + horizon]
            mfe = max(float(row[2]) - sample_entry for row in future)
            mae = max(sample_entry - float(row[3]) for row in future)
            outcome = "TIMEOUT"
            for row in future:
                high = float(row[2])
                low = float(row[3])
                tp_hit = high >= sample_tp
                sl_hit = low <= sample_stop
                if tp_hit and sl_hit:
                    outcome = "STOP_LOSS"
                    break
                if sl_hit:
                    outcome = "STOP_LOSS"
                    break
                if tp_hit:
                    outcome = "TARGET_HIT"
                    break
            outcomes.append({"outcome": outcome, "mfe": mfe, "mae": mae})
        return outcomes

    @staticmethod
    def percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, math.ceil((percentile / 100.0) * len(ordered)) - 1))
        return ordered[index]

    def validate_signal_setup(
        self,
        best: dict[str, Any],
        entry: float,
        stop_loss: float,
        take_profit: float,
        economics: dict[str, float | bool],
        volatility: dict[str, float],
        probability_context: dict[str, Any],
    ) -> dict[str, str]:
        stop_noise_floor = max(
            volatility["atr"] * self.min_stop_atr_ratio,
            volatility["avg_range"] * self.min_stop_atr_ratio,
            volatility["spread_price"] * self.min_stop_spread_multiple,
        )
        if volatility["stop_distance"] < stop_noise_floor:
            return {"status": "REJECTED", "reason": "STOP_TOO_TIGHT_FOR_VOLATILITY"}
        if volatility["tp_atr_ratio"] > self.max_tp_atr_multiple:
            return {"status": "REJECTED", "reason": "TP_TOO_FAR_FOR_ATR"}
        if probability_context["sample_size"] >= self.min_probability_sample_size and volatility["tp_distance"] > probability_context["mfe_percentile"]:
            return {"status": "REJECTED", "reason": "TP_ABOVE_HISTORICAL_MFE_PERCENTILE"}
        if take_profit != take_profit:
            return {"status": "REJECTED", "reason": "INVALID_TAKE_PROFIT"}
        if economics["gross_rr"] > 10.0:
            self.logger.warning(
                "SIGNAL_VALIDATION_WARNING reason=EXTREME_GROSS_RR strategy=%s gross_rr=%.4f net_rr=%.4f tp_atr_ratio=%.4f sample=%s",
                best["strategy"],
                economics["gross_rr"],
                economics["net_rr"],
                volatility["tp_atr_ratio"],
                probability_context["sample_size"],
            )
        return {"status": "PASSED", "reason": "OK"}

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
            "ECONOMIC_CALC_DEBUG signal_id=%s pair=%s entry=%.6f stop=%.6f technical_tp=%.6f effective_tp=%.6f stop_distance=%.6f stop_pct=%.4f atr=%.6f stop_atr_ratio=%.4f tp_atr_ratio=%.4f capital_eur=%.6f capital_quote=%.6f quantity=%.8f entry_notional=%.6f tp_notional=%.6f sl_notional=%.6f gross_profit_tp=%.6f gross_loss_sl=%.6f buy_fee=%.6f sell_fee_tp=%.6f sell_fee_sl=%.6f spread_cost=%.6f slippage_cost=%.6f net_profit_tp=%.6f net_loss_sl=%.6f gross_rr=%.4f net_rr=%.4f sample=%s win_rate=%s mfe_percentile=%.6f mae_percentile=%.6f validation=%s reason=%s",
            signal_id, signal.pair, signal.entry, signal.stop_loss, signal.technical_take_profit, signal.effective_take_profit,
            signal.stop_distance, signal.stop_pct, signal.atr, signal.stop_atr_ratio, signal.tp_atr_ratio,
            self.trade_notional_eur, self.trade_notional_eur, signal.quantity, signal.entry_notional_eur, signal.tp_notional_eur, signal.sl_notional_eur,
            signal.gross_profit_tp1_eur, signal.gross_loss_sl_eur, signal.estimated_buy_fee_eur,
            signal.estimated_sell_fee_eur, signal.estimated_sell_fee_sl_eur, signal.estimated_spread_cost_eur,
            signal.estimated_slippage_cost_eur, signal.net_profit_tp1_eur, signal.net_loss_sl_eur, signal.gross_rr, signal.net_rr,
            signal.historical_sample_size, signal.historical_win_rate, signal.historical_mfe_percentile, signal.historical_mae_percentile,
            signal.validation_status, signal.validation_reason,
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
