"""Relative-strength intraday PAPER strategy inspired by the thesis methodology.

Ranks crypto assets against BTC on closed 1h/15m candles, then opens LONG paper
positions in the strongest assets and SHORT paper positions in the weakest.
No TRIX or Ichimoku dependency is used.
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from project.simple_strategy_scanner import SimpleStrategyScanner
from project.strategy_engine.service import GeneratedSignal

STRATEGY_NAME = "Relative Strength Crypto vs BTC"
RUNTIME_VERSION = "RELATIVE_STRENGTH_BTC_1H_15M_V1"


class RelativeStrengthScanner(SimpleStrategyScanner):
    def __init__(
        self,
        *args: Any,
        strongest_count: int = 3,
        weakest_count: int = 3,
        max_signals_per_day: int = 12,
        max_open_positions: int = 4,
        max_per_pair_day: int = 2,
        min_abs_score: float = 0.35,
        volume_ratio_min: float = 0.70,
        stop_atr: float = 1.20,
        target_r: float = 1.50,
        max_hold_candles: int = 8,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.strongest_count = max(1, int(strongest_count))
        self.weakest_count = max(1, int(weakest_count))
        self.max_signals_per_day = max(1, int(max_signals_per_day))
        self.max_open_positions = max(1, int(max_open_positions))
        self.max_per_pair_day = max(1, int(max_per_pair_day))
        self.min_abs_score = max(0.0, float(min_abs_score))
        self.volume_ratio_min = max(0.1, float(volume_ratio_min))
        self.stop_atr = max(0.5, float(stop_atr))
        self.target_r = max(0.5, float(target_r))
        self.max_hold_candles = max(1, int(max_hold_candles))

    def _count(self, *, open_only: bool = False, today_only: bool = False) -> int:
        clauses = ["score_breakdown->>'runtime_version' = %s"]
        params: list[Any] = [RUNTIME_VERSION]
        if open_only:
            clauses.append("status IN ('NEW', 'OPEN')")
        if today_only:
            clauses.append("telegram_sent_at IS NOT NULL")
            clauses.append("(created_at AT TIME ZONE 'Europe/Rome')::date = (NOW() AT TIME ZONE 'Europe/Rome')::date")
        rows = self.client.fetch_all(
            f"SELECT COUNT(*) FROM signals.generated_signals WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        return int(rows[0][0] or 0) if rows else 0

    def _pair_today(self, pair: str) -> int:
        rows = self.client.fetch_all(
            """SELECT COUNT(*) FROM signals.generated_signals
               WHERE pair = %s AND score_breakdown->>'runtime_version' = %s
                 AND telegram_sent_at IS NOT NULL
                 AND (created_at AT TIME ZONE 'Europe/Rome')::date = (NOW() AT TIME ZONE 'Europe/Rome')::date""",
            (pair, RUNTIME_VERSION),
        )
        return int(rows[0][0] or 0) if rows else 0

    def find_active_duplicate(self, candidate: GeneratedSignal) -> int | None:
        direction = str(candidate.score_breakdown.get("direction", "LONG"))
        rows = self.client.fetch_all(
            """SELECT id FROM signals.generated_signals
               WHERE pair = %s AND score_breakdown->>'direction' = %s
                 AND score_breakdown->>'runtime_version' = %s
                 AND created_at >= NOW() - INTERVAL '60 minutes'
               ORDER BY created_at DESC LIMIT 1""",
            (candidate.pair, direction, RUNTIME_VERSION),
        )
        if rows:
            return int(rows[0][0])
        return -1 if self._pair_today(candidate.pair) >= self.max_per_pair_day else None

    def evaluate(self) -> GeneratedSignal | None:
        sent_today = self._count(today_only=True)
        open_positions = self._count(open_only=True)
        if sent_today >= self.max_signals_per_day or open_positions >= self.max_open_positions:
            return None

        btc_15m = self._frame("BTC/USD", "15m", 160)
        btc_1h = self._frame("BTC/USD", "1h", 120)
        if len(btc_15m) < 40 or len(btc_1h) < 30:
            return None

        ranked: list[dict[str, Any]] = []
        for pair in self.pairs:
            if pair == "BTC/USD":
                continue
            try:
                item = self._rank_pair(pair, btc_15m, btc_1h)
            except Exception:
                self.logger.exception("RELATIVE_STRENGTH pair_failed pair=%s", pair)
                continue
            if item is not None:
                ranked.append(item)

        ranked.sort(key=lambda value: float(value["rs_score"]), reverse=True)
        selections: list[tuple[dict[str, Any], str, int]] = []
        for rank, item in enumerate(ranked[: self.strongest_count], start=1):
            if float(item["rs_score"]) >= self.min_abs_score:
                selections.append((item, "LONG", rank))
        weak = list(reversed(ranked[-self.weakest_count :]))
        for rank, item in enumerate(weak, start=1):
            if float(item["rs_score"]) <= -self.min_abs_score:
                selections.append((item, "SHORT", rank))

        candidates: list[GeneratedSignal] = []
        for item, direction, rank in selections:
            candidate = self._build_candidate(item, direction, rank, len(ranked))
            if candidate is not None:
                candidates.append(candidate)
        candidates.sort(key=lambda signal: abs(float(signal.score_breakdown["rs_score"])), reverse=True)

        limit = min(
            self.max_signals_per_cycle,
            self.max_signals_per_day - sent_today,
            self.max_open_positions - open_positions,
        )
        published: list[GeneratedSignal] = []
        for candidate in candidates:
            if len(published) >= limit:
                break
            if self.find_active_duplicate(candidate) is not None:
                continue
            signal_id = self.save_signal(candidate)
            signal = replace(candidate, signal_id=signal_id)
            self.publish_signal_event(signal)
            published.append(signal)
            self.logger.info(
                "RELATIVE_STRENGTH_SIGNAL id=%s pair=%s direction=%s score=%.4f rank=%s",
                signal_id,
                signal.pair,
                signal.score_breakdown.get("direction"),
                signal.score_breakdown.get("rs_score"),
                signal.score_breakdown.get("rank"),
            )
        return published[0] if published else None

    def _rank_pair(self, pair: str, btc_15m: pd.DataFrame, btc_1h: pd.DataFrame) -> dict[str, Any] | None:
        asset_15m = self._frame(pair, "15m", 160)
        asset_1h = self._frame(pair, "1h", 120)
        if len(asset_15m) < 40 or len(asset_1h) < 30:
            return None
        a15, b15 = self._indicators(asset_15m), self._indicators(btc_15m)
        a1, b1 = self._indicators(asset_1h), self._indicators(btc_1h)
        latest = a15.iloc[-1]
        volume_ratio = float(latest["volume_ratio"])
        if pd.isna(volume_ratio) or volume_ratio < self.volume_ratio_min:
            return None

        ret_1h = self._ret(a15, 4) - self._ret(b15, 4)
        ret_4h = self._ret(a1, 4) - self._ret(b1, 4)
        ret_15m = self._ret(a15, 1) - self._ret(b15, 1)
        ratio = (a15["close"].tail(40).reset_index(drop=True) / b15["close"].tail(40).reset_index(drop=True))
        ratio_momentum = float(ratio.iloc[-1] / ratio.iloc[-5] - 1.0) if len(ratio) >= 5 else 0.0
        trend_component = (float(latest["close"]) / float(latest["ema20"]) - 1.0)
        rs_score = (
            0.35 * ret_4h * 100.0
            + 0.25 * ret_1h * 100.0
            + 0.15 * ret_15m * 100.0
            + 0.15 * ratio_momentum * 100.0
            + 0.05 * (volume_ratio - 1.0)
            + 0.05 * trend_component * 100.0
        )
        return {
            "pair": pair,
            "asset_15m": a15,
            "rs_score": rs_score,
            "relative_4h": ret_4h,
            "relative_1h": ret_1h,
            "relative_15m": ret_15m,
            "ratio_momentum": ratio_momentum,
            "volume_ratio": volume_ratio,
        }

    def _build_candidate(self, item: dict[str, Any], direction: str, rank: int, universe_size: int) -> GeneratedSignal | None:
        frame = item["asset_15m"]
        latest = frame.iloc[-1]
        entry = float(latest["close"])
        atr = float(latest["atr"])
        ema20 = float(latest["ema20"])
        if atr <= 0:
            return None
        candle_ok = float(latest["close"]) > float(latest["open"]) if direction == "LONG" else float(latest["close"]) < float(latest["open"])
        trend_ok = entry > ema20 if direction == "LONG" else entry < ema20
        if not candle_ok or not trend_ok:
            return None
        distance = atr * self.stop_atr
        stop = entry - distance if direction == "LONG" else entry + distance
        target = entry + distance * self.target_r if direction == "LONG" else entry - distance * self.target_r
        if target <= 0:
            return None
        economics = self._economics(direction, entry, stop, target)
        if not bool(economics.get("tradable")) or float(economics["net_profit_tp1_eur"]) <= 0:
            return None
        rs_score = float(item["rs_score"])
        confidence = min(100.0, 55.0 + abs(rs_score) * 12.0 + min(12.0, max(0.0, float(item["volume_ratio"]) - 0.7) * 10.0))
        reasons = [
            f"rank {rank}/{universe_size} tra le crypto {'più forti' if direction == 'LONG' else 'più deboli'} contro BTC",
            f"forza relativa 4h={float(item['relative_4h']) * 100:+.2f}%",
            f"forza relativa 1h={float(item['relative_1h']) * 100:+.2f}%",
            f"momentum rapporto asset/BTC={float(item['ratio_momentum']) * 100:+.2f}%",
            f"volume 15m={float(item['volume_ratio']):.2f}x mediana",
        ]
        breakdown: dict[str, Any] = {
            "runtime_version": RUNTIME_VERSION,
            "strategy_version": "RELATIVE_STRENGTH_V1",
            "direction": direction,
            "rank": rank,
            "universe_size": universe_size,
            "rs_score": round(rs_score, 6),
            "relative_4h": round(float(item["relative_4h"]), 8),
            "relative_1h": round(float(item["relative_1h"]), 8),
            "relative_15m": round(float(item["relative_15m"]), 8),
            "ratio_momentum": round(float(item["ratio_momentum"]), 8),
            "volume_ratio": round(float(item["volume_ratio"]), 4),
            "risk_distance": round(distance, 10),
            "initial_stop": round(stop, 10),
            "target_price": round(target, 10),
            "max_hold_candles": self.max_hold_candles,
            "paper_only": True,
        }
        return GeneratedSignal(
            strategy=STRATEGY_NAME,
            pair=str(item["pair"]),
            timeframe="15m",
            regime=f"RELATIVE_STRENGTH_{direction}",
            entry=entry,
            stop_loss=stop,
            take_profit=target,
            technical_take_profit=target,
            effective_take_profit=target,
            signal_time=datetime.now(timezone.utc),
            reference_candle_time=pd.Timestamp(latest["timestamp"]).to_pydatetime(),
            entry_timing="ENTER_AFTER_CLOSED_15M_CANDLE",
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
            stop_distance=distance,
            stop_pct=distance / entry * 100.0,
            atr=atr,
            stop_atr_ratio=self.stop_atr,
            tp_atr_ratio=self.stop_atr * self.target_r,
            historical_sample_size=0,
            historical_win_rate=None,
            historical_expected_value_eur=None,
            historical_mfe_percentile=0.0,
            historical_mae_percentile=0.0,
            probability_confidence="FORWARD_TEST",
            validation_status="RELATIVE_STRENGTH_RULES_PASSED",
            validation_reason="; ".join(reasons),
            signal_class="B",
            score=confidence,
            probability=None,
            reasons=reasons,
            score_breakdown=breakdown,  # type: ignore[arg-type]
        )

    def _economics(self, direction: str, entry: float, stop: float, target: float) -> dict[str, float | bool]:
        step = max(float(self.quantity_step), 1e-12)
        quantity = math.floor((self.trade_notional_eur / entry) / step) * step
        entry_notional = quantity * entry
        if quantity < self.min_qty or entry_notional < self.min_notional_eur:
            return {"tradable": False}
        target_notional, stop_notional = quantity * target, quantity * stop
        gross_profit = quantity * ((target - entry) if direction == "LONG" else (entry - target))
        gross_loss = quantity * ((entry - stop) if direction == "LONG" else (stop - entry))
        entry_fee = entry_notional * self.buy_fee_rate
        target_fee, stop_fee = target_notional * self.sell_fee_rate, stop_notional * self.sell_fee_rate
        target_friction = quantity * (entry + target) * ((self.spread_rate / 2.0) + self.slippage_rate)
        stop_friction = quantity * (entry + stop) * ((self.spread_rate / 2.0) + self.slippage_rate)
        net_profit = gross_profit - entry_fee - target_fee - target_friction
        net_loss = gross_loss + entry_fee + stop_fee + stop_friction
        return {
            "tradable": True, "quantity": quantity, "entry_notional_eur": entry_notional,
            "tp_notional_eur": target_notional, "sl_notional_eur": stop_notional,
            "gross_profit_tp1_eur": gross_profit, "gross_loss_sl_eur": gross_loss,
            "estimated_buy_fee_eur": entry_fee, "estimated_sell_fee_eur": target_fee,
            "estimated_sell_fee_sl_eur": stop_fee, "estimated_spread_cost_eur": target_friction,
            "estimated_slippage_cost_eur": 0.0, "net_profit_tp1_eur": net_profit,
            "net_loss_sl_eur": net_loss, "gross_rr": gross_profit / gross_loss if gross_loss > 0 else 0.0,
            "net_rr": net_profit / net_loss if net_loss > 0 else 0.0,
        }

    def _frame(self, pair: str, timeframe: str, limit: int) -> pd.DataFrame:
        seconds = {"15m": 900, "1h": 3600}[timeframe]
        rows = self.client.fetch_all(
            """SELECT timestamp, open, high, low, close, volume FROM market_data.ohlc
               WHERE LOWER(TRIM(exchange)) = LOWER(TRIM(%s)) AND pair = %s AND timeframe = %s
               ORDER BY timestamp DESC LIMIT %s""",
            (self.exchange, pair, timeframe, limit + 4),
        )
        frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        if frame.empty:
            return frame
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        for column in ["open", "high", "low", "close", "volume"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        end = frame["timestamp"].astype("int64") // 1_000_000_000 + seconds
        return frame[end <= pd.Timestamp.now(tz="UTC").timestamp()].sort_values("timestamp").tail(limit).reset_index(drop=True)

    @staticmethod
    def _ret(frame: pd.DataFrame, periods: int) -> float:
        if len(frame) <= periods:
            return 0.0
        return float(frame["close"].iloc[-1] / frame["close"].iloc[-1 - periods] - 1.0)

    @staticmethod
    def _indicators(frame: pd.DataFrame) -> pd.DataFrame:
        data = frame.copy()
        close, high, low = data["close"].astype(float), data["high"].astype(float), data["low"].astype(float)
        data["ema20"] = close.ewm(span=20, adjust=False).mean()
        previous_close = close.shift(1)
        tr = pd.concat([high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1).max(axis=1)
        data["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
        median = data["volume"].rolling(20).median().replace(0, float("nan"))
        data["volume_ratio"] = data["volume"] / median
        return data
