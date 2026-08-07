"""Clean 15m Velez-style pullback/restart PAPER scanner.

This module deliberately does not depend on Relative Strength, TRIX or Ichimoku.
The rules are objective adaptations of Velez's published intraday principles:
20/200 moving-average structure, pullback, then proof of strength/weakness through
breaking the prior 15m bar. It is a codified interpretation of the published
method, not a claim of a verbatim transcription of a specific video.
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from project.simple_strategy_scanner import SimpleStrategyScanner
from project.strategy_engine.service import GeneratedSignal

STRATEGY_NAME = "Oliver Velez 15m Pullback Restart"
RUNTIME_VERSION = "VELEZ_15M_PULLBACK_RESTART_V1"


class Velez15mScanner(SimpleStrategyScanner):
    def __init__(
        self,
        *args: Any,
        max_signals_per_day: int = 8,
        max_open_positions: int = 4,
        max_per_pair_day: int = 2,
        target_r: float = 1.5,
        max_hold_candles: int = 12,
        pullback_bars: int = 3,
        touch_atr: float = 0.35,
        **kwargs: Any,
    ) -> None:
        kwargs["max_signals_per_cycle"] = min(int(kwargs.get("max_signals_per_cycle", 2)), 2)
        kwargs["min_signal_score"] = 0.0
        kwargs["min_net_rr"] = 0.0
        kwargs["min_net_profit_eur"] = 0.0
        kwargs["enable_daily_signal_report"] = False
        super().__init__(*args, **kwargs)
        self.max_signals_per_day = max(1, int(max_signals_per_day))
        self.max_open_positions = max(1, int(max_open_positions))
        self.max_per_pair_day = max(1, int(max_per_pair_day))
        self.target_r = max(1.0, float(target_r))
        self.max_hold_candles = max(4, int(max_hold_candles))
        self.pullback_bars = max(3, int(pullback_bars))
        self.touch_atr = max(0.05, float(touch_atr))
        self.logger.name = "velez-15m"

    def _count(self, *, open_only: bool = False, today_only: bool = False) -> int:
        clauses = ["score_breakdown->>'runtime_version' = %s"]
        params: list[Any] = [RUNTIME_VERSION]
        if open_only:
            clauses.append("status IN ('NEW','OPEN')")
        if today_only:
            clauses.append("telegram_sent_at IS NOT NULL")
            clauses.append("(created_at AT TIME ZONE 'Europe/Rome')::date=(NOW() AT TIME ZONE 'Europe/Rome')::date")
        rows = self.client.fetch_all(
            f"SELECT COUNT(*) FROM signals.generated_signals WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        return int(rows[0][0] or 0) if rows else 0

    def _pair_today(self, pair: str) -> int:
        rows = self.client.fetch_all(
            """SELECT COUNT(*) FROM signals.generated_signals
               WHERE pair=%s AND score_breakdown->>'runtime_version'=%s
                 AND telegram_sent_at IS NOT NULL
                 AND (created_at AT TIME ZONE 'Europe/Rome')::date=(NOW() AT TIME ZONE 'Europe/Rome')::date""",
            (pair, RUNTIME_VERSION),
        )
        return int(rows[0][0] or 0) if rows else 0

    def find_active_duplicate(self, candidate: GeneratedSignal) -> int | None:
        direction = str(candidate.score_breakdown.get("direction", "LONG"))
        rows = self.client.fetch_all(
            """SELECT id FROM signals.generated_signals
               WHERE pair=%s AND score_breakdown->>'direction'=%s
                 AND score_breakdown->>'runtime_version'=%s
                 AND status IN ('NEW','OPEN')
               ORDER BY created_at DESC LIMIT 1""",
            (candidate.pair, direction, RUNTIME_VERSION),
        )
        if rows:
            return int(rows[0][0])
        return -1 if self._pair_today(candidate.pair) >= self.max_per_pair_day else None

    @staticmethod
    def _ma(frame: pd.DataFrame) -> pd.DataFrame:
        data = frame.copy()
        data["sma20"] = data["close"].rolling(20).mean()
        data["sma200"] = data["close"].rolling(200).mean()
        tr = pd.concat(
            [
                data["high"] - data["low"],
                (data["high"] - data["close"].shift(1)).abs(),
                (data["low"] - data["close"].shift(1)).abs(),
            ],
            axis=1,
        ).max(axis=1)
        data["atr"] = tr.rolling(14).mean()
        return data

    def evaluate(self) -> GeneratedSignal | None:
        sent_today = self._count(today_only=True)
        open_count = self._count(open_only=True)
        if sent_today >= self.max_signals_per_day:
            self.logger.info("VELEZ_BLOCK daily_limit sent=%s", sent_today)
            return None
        if open_count >= self.max_open_positions:
            self.logger.info("VELEZ_BLOCK max_open open=%s", open_count)
            return None

        qualified: list[GeneratedSignal] = []
        reasons: dict[str, int] = {}
        for pair in self.pairs:
            try:
                candidate, reason = self.evaluate_pair(pair)
            except Exception:
                self.logger.exception("VELEZ_PAIR_FAILED pair=%s", pair)
                candidate, reason = None, "PAIR_ERROR"
            if candidate is None:
                reasons[reason] = reasons.get(reason, 0) + 1
            else:
                qualified.append(candidate)

        qualified.sort(key=lambda item: float(item.score), reverse=True)
        limit = min(
            self.max_signals_per_cycle,
            self.max_signals_per_day - sent_today,
            self.max_open_positions - open_count,
        )
        published: list[GeneratedSignal] = []
        for candidate in qualified:
            if len(published) >= limit:
                break
            if self.find_active_duplicate(candidate) is not None:
                reasons["DUPLICATE_OR_PAIR_LIMIT"] = reasons.get("DUPLICATE_OR_PAIR_LIMIT", 0) + 1
                continue
            signal_id = self.save_signal(candidate)
            signal = replace(candidate, signal_id=signal_id)
            self.publish_signal_event(signal)
            published.append(signal)
            self.logger.info(
                "VELEZ_SIGNAL id=%s pair=%s direction=%s entry=%.8f stop=%.8f target=%.8f score=%.1f",
                signal_id,
                signal.pair,
                signal.score_breakdown.get("direction"),
                signal.entry,
                signal.stop_loss,
                signal.take_profit,
                signal.score,
            )
        self.logger.info(
            "VELEZ_AUDIT universe=%s qualified=%s published=%s rejections=%s",
            len(self.pairs), len(qualified), len(published), reasons,
        )
        return published[0] if published else None

    def evaluate_pair(self, pair: str) -> tuple[GeneratedSignal | None, str]:
        raw = self.fetch_closed_frame(pair, "15m", 280)
        if len(raw) < 220:
            return None, "INSUFFICIENT_OHLC"
        frame = self._ma(raw).dropna().reset_index(drop=True)
        if len(frame) < 205:
            return None, "INSUFFICIENT_MA_HISTORY"

        latest = frame.iloc[-1]
        previous = frame.iloc[-2]
        pullback = frame.iloc[-1-self.pullback_bars:-1]
        atr = float(latest["atr"])
        if atr <= 0:
            return None, "ATR_INVALID"

        long_trend = bool(
            latest["sma20"] > latest["sma200"]
            and frame.iloc[-1]["sma20"] > frame.iloc[-3]["sma20"]
            and float(latest["close"]) > float(latest["sma200"])
        )
        short_trend = bool(
            latest["sma20"] < latest["sma200"]
            and frame.iloc[-1]["sma20"] < frame.iloc[-3]["sma20"]
            and float(latest["close"]) < float(latest["sma200"])
        )
        if not long_trend and not short_trend:
            return None, "NO_20_200_TREND"

        if long_trend:
            direction = "LONG"
            ordered_pullback = all(
                float(pullback.iloc[i]["high"]) >= float(pullback.iloc[i + 1]["high"])
                for i in range(len(pullback) - 1)
            )
            touched20 = bool((pullback["low"] <= pullback["sma20"] + self.touch_atr * pullback["atr"]).any())
            held_structure = bool((pullback["close"] > pullback["sma200"]).all())
            trigger = bool(
                float(latest["close"]) > float(latest["open"])
                and float(latest["high"]) > float(previous["high"])
                and float(latest["close"]) > float(previous["high"])
                and float(latest["close"]) > float(latest["sma20"])
            )
        else:
            direction = "SHORT"
            ordered_pullback = all(
                float(pullback.iloc[i]["low"]) <= float(pullback.iloc[i + 1]["low"])
                for i in range(len(pullback) - 1)
            )
            touched20 = bool((pullback["high"] >= pullback["sma20"] - self.touch_atr * pullback["atr"]).any())
            held_structure = bool((pullback["close"] < pullback["sma200"]).all())
            trigger = bool(
                float(latest["close"]) < float(latest["open"])
                and float(latest["low"]) < float(previous["low"])
                and float(latest["close"]) < float(previous["low"])
                and float(latest["close"]) < float(latest["sma20"])
            )

        if not ordered_pullback:
            return None, "PULLBACK_NOT_ORDERED"
        if not touched20:
            return None, "NO_PULLBACK_TO_20"
        if not held_structure:
            return None, "STRUCTURE_FAILED"
        if not trigger:
            return None, "NO_TRIGGER_BAR_BREAK"

        entry = float(latest["close"])
        pullback_low = float(pullback["low"].min())
        pullback_high = float(pullback["high"].max())
        buffer = max(entry * 0.0005, atr * 0.10)
        stop = (pullback_low - buffer) if direction == "LONG" else (pullback_high + buffer)
        risk = (entry - stop) if direction == "LONG" else (stop - entry)
        if risk <= 0 or risk > 2.5 * atr:
            return None, "STOP_GEOMETRY_INVALID"
        target = entry + self.target_r * risk if direction == "LONG" else entry - self.target_r * risk
        if target <= 0:
            return None, "TARGET_INVALID"

        economics = self._economics(direction, entry, stop, target)
        if not bool(economics.get("tradable")):
            return None, "NOT_TRADABLE"
        if float(economics["net_profit_tp1_eur"]) <= 0:
            return None, "TARGET_NOT_NET_POSITIVE"

        slope20 = abs(float(frame.iloc[-1]["sma20"] / frame.iloc[-3]["sma20"] - 1.0))
        separation = abs(float(latest["sma20"] / latest["sma200"] - 1.0))
        score = min(100.0, 65.0 + min(15.0, slope20 * 8000.0) + min(15.0, separation * 1200.0) + 5.0)
        reasons = [
            "20/200 allineate sul timeframe 15m",
            f"pullback ordinato di {self.pullback_bars} barre verso SMA20",
            "barra trigger supera il massimo/minimo della barra precedente",
            "stop oltre l'estremo del pullback",
            "gestione sullo stesso timeframe 15m",
        ]
        breakdown = {
            "runtime_version": RUNTIME_VERSION,
            "strategy_version": "VELEZ_15M_V1",
            "direction": direction,
            "setup_type": "PULLBACK_RESTART_PRIOR_BAR_BREAK",
            "sma20": round(float(latest["sma20"]), 10),
            "sma200": round(float(latest["sma200"]), 10),
            "atr": round(atr, 10),
            "risk_distance": round(risk, 10),
            "initial_stop": round(stop, 10),
            "target_price": round(target, 10),
            "max_hold_candles": self.max_hold_candles,
            "breakeven_trigger_r": 0.8,
            "trailing_trigger_r": 1.0,
            "trailing_distance_r": 0.75,
            "paper_only": True,
        }
        now = datetime.now(timezone.utc)
        return GeneratedSignal(
            strategy=STRATEGY_NAME,
            pair=pair,
            timeframe="15m",
            regime=f"VELEZ_{direction}",
            entry=entry,
            stop_loss=stop,
            take_profit=target,
            technical_take_profit=target,
            effective_take_profit=target,
            signal_time=now,
            reference_candle_time=pd.Timestamp(latest["timestamp"]).to_pydatetime(),
            entry_timing="ENTER_AFTER_CLOSED_15M_TRIGGER_BAR",
            quantity=float(economics["quantity"]),
            entry_notional_eur=float(economics["entry_notional_eur"]),
            tp_notional_eur=float(economics["tp_notional_eur"]),
            sl_notional_eur=float(economics["sl_notional_eur"]),
            gross_profit_tp1_eur=float(economics["gross_profit_tp1_eur"]),
            gross_loss_sl_eur=float(economics["gross_loss_sl_eur"]),
            estimated_buy_fee_eur=float(economics["estimated_buy_fee_eur"]),
            estimated_sell_fee_eur=float(economics["estimated_sell_fee_eur"]),
            estimated_sell_fee_sl_eur=float(economics["estimated_sell_fee_sl_eur"]),
            estimated_spread_cost_eur=float(economics["estimated_spread_cost_eur"]),
            estimated_slippage_cost_eur=float(economics["estimated_slippage_cost_eur"]),
            net_profit_tp1_eur=float(economics["net_profit_tp1_eur"]),
            net_loss_sl_eur=float(economics["net_loss_sl_eur"]),
            gross_rr=float(economics["gross_rr"]),
            net_rr=float(economics["net_rr"]),
            stop_distance=risk,
            stop_pct=risk / entry * 100.0,
            atr=atr,
            stop_atr_ratio=risk / atr,
            tp_atr_ratio=(self.target_r * risk) / atr,
            historical_sample_size=0,
            historical_win_rate=None,
            historical_expected_value_eur=None,
            historical_mfe_percentile=0.0,
            historical_mae_percentile=0.0,
            probability_confidence="FORWARD_TEST",
            validation_status="VELEZ_RULES_PASSED",
            validation_reason="; ".join(reasons),
            signal_class="B",
            score=score,
            probability=None,
            reasons=reasons,
            score_breakdown=breakdown,
        ), "QUALIFIED"

    def _economics(self, direction: str, entry: float, stop: float, target: float) -> dict[str, float | bool]:
        step = max(float(self.quantity_step), 1e-12)
        quantity = math.floor((self.trade_notional_eur / entry) / step) * step
        entry_notional = quantity * entry
        if quantity < self.min_qty or entry_notional < self.min_notional_eur:
            return {"tradable": False}
        target_notional = quantity * target
        stop_notional = quantity * stop
        gross_profit = quantity * ((target - entry) if direction == "LONG" else (entry - target))
        gross_loss = quantity * ((entry - stop) if direction == "LONG" else (stop - entry))
        entry_fee = entry_notional * self.buy_fee_rate
        target_fee = target_notional * self.sell_fee_rate
        stop_fee = stop_notional * self.sell_fee_rate
        target_friction = quantity * (entry + target) * ((self.spread_rate / 2.0) + self.slippage_rate)
        stop_friction = quantity * (entry + stop) * ((self.spread_rate / 2.0) + self.slippage_rate)
        net_profit = gross_profit - entry_fee - target_fee - target_friction
        net_loss = gross_loss + entry_fee + stop_fee + stop_friction
        return {
            "tradable": True,
            "quantity": quantity,
            "entry_notional_eur": entry_notional,
            "tp_notional_eur": target_notional,
            "sl_notional_eur": stop_notional,
            "gross_profit_tp1_eur": gross_profit,
            "gross_loss_sl_eur": gross_loss,
            "estimated_buy_fee_eur": entry_fee,
            "estimated_sell_fee_eur": target_fee,
            "estimated_sell_fee_sl_eur": stop_fee,
            "estimated_spread_cost_eur": target_friction,
            "estimated_slippage_cost_eur": 0.0,
            "net_profit_tp1_eur": net_profit,
            "net_loss_sl_eur": net_loss,
            "gross_rr": gross_profit / gross_loss if gross_loss > 0 else 0.0,
            "net_rr": net_profit / net_loss if net_loss > 0 else 0.0,
        }
