"""Simple, readable crypto scanner without research grids or probabilistic classes.

The scanner evaluates twenty spot pairs on closed 15m candles, uses 1h context, selects
one of three explicit LONG strategies, and publishes only actionable Telegram signals.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from project.shared.events import Event, EventBus, EventType
from project.shared.logging import get_module_logger
from project.strategy_engine.service import GeneratedSignal, StrategyEngine


_TIMEFRAME_SECONDS = {"15m": 900, "1h": 3600}


class _ClientRepository:
    """Minimal adapter required by StrategyEngine persistence helpers."""

    def __init__(self, client: Any) -> None:
        self.client = client


class SimpleStrategyScanner(StrategyEngine):
    """Scan fixed markets with three transparent rule-based strategies."""

    def __init__(
        self,
        client: Any,
        event_bus: EventBus,
        pairs: list[str],
        exchange: str = "Kraken",
        trade_notional_eur: float = 100.0,
        buy_fee_rate: float = 0.001,
        sell_fee_rate: float = 0.001,
        spread_rate: float = 0.0005,
        slippage_rate: float = 0.0,
        quantity_step: float = 0.000001,
        min_qty: float = 0.0,
        min_notional_eur: float = 10.0,
        signal_cooldown_minutes: int = 90,
        min_signal_score: float = 70.0,
        min_net_rr: float = 1.20,
        min_net_profit_eur: float = 0.30,
        max_signals_per_cycle: int = 3,
        enable_daily_signal_report: bool = True,
        daily_signal_report_hours: int = 24,
    ) -> None:
        super().__init__(
            _ClientRepository(client),
            decision_engine=None,  # type: ignore[arg-type]
            event_bus=event_bus,
            operational_timeframe="15m",
            trade_notional_eur=trade_notional_eur,
            buy_fee_rate=buy_fee_rate,
            sell_fee_rate=sell_fee_rate,
            spread_rate=spread_rate,
            slippage_rate=slippage_rate,
            quantity_step=quantity_step,
            min_qty=min_qty,
            min_notional_eur=min_notional_eur,
            signal_cooldown_minutes=signal_cooldown_minutes,
            min_net_rr=min_net_rr,
            min_tp1_net_profit_eur=min_net_profit_eur,
            operative_signal_classes=["B"],
            watchlist_signal_classes=[],
            enable_watchlist_alerts=False,
            enable_daily_signal_report=enable_daily_signal_report,
            daily_signal_report_hours=daily_signal_report_hours,
        )
        self.client = client
        self.pairs = list(dict.fromkeys(pair.upper() for pair in pairs))
        self.exchange = exchange
        self.min_signal_score = float(min_signal_score)
        self.max_signals_per_cycle = max(1, int(max_signals_per_cycle))
        self.logger = get_module_logger("simple-scanner")
        self._last_cycle_summary: dict[str, Any] = {}

    def evaluate(self) -> GeneratedSignal | None:
        candidates: list[GeneratedSignal] = []
        rejection_reasons: dict[str, int] = {}

        for pair in self.pairs:
            try:
                candidate, reason = self.evaluate_pair(pair)
            except Exception:
                self.logger.exception("SIMPLE_SCANNER pair_failed pair=%s", pair)
                candidate, reason = None, "PAIR_ERROR"
            if candidate is not None:
                candidates.append(candidate)
            else:
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

        candidates.sort(key=lambda item: item.score, reverse=True)
        published: list[GeneratedSignal] = []
        for candidate in candidates:
            if len(published) >= self.max_signals_per_cycle:
                break
            duplicate_id = self.find_active_duplicate(candidate)
            if duplicate_id is not None:
                rejection_reasons["ACTIVE_PAIR_COOLDOWN"] = rejection_reasons.get("ACTIVE_PAIR_COOLDOWN", 0) + 1
                self.logger.info(
                    "SIMPLE_SIGNAL_SKIPPED pair=%s reason=ACTIVE_PAIR_COOLDOWN existing_id=%s",
                    candidate.pair,
                    duplicate_id,
                )
                continue
            signal_id = self.save_signal(candidate)
            signal = replace(candidate, signal_id=signal_id)
            self.publish_signal_event(signal)
            published.append(signal)
            self.logger.info(
                "SIMPLE_SIGNAL_SENT id=%s pair=%s strategy=%s score=%.1f entry=%.8f stop=%.8f target=%.8f net_rr=%.3f",
                signal_id,
                signal.pair,
                signal.strategy,
                signal.score,
                signal.entry,
                signal.stop_loss,
                signal.take_profit,
                signal.net_rr,
            )

        best = candidates[0] if candidates else None
        self._last_cycle_summary = {
            "pairs_scanned": len(self.pairs),
            "qualified": len(candidates),
            "published": len(published),
            "best_candidate": (
                f"{best.pair} {best.strategy} score={best.score:.1f}" if best else "NONE"
            ),
            "rejections": rejection_reasons,
        }
        self.logger.info(
            "SIMPLE_SCANNER_SUMMARY pairs=%s qualified=%s published=%s best=%s rejections=%s",
            len(self.pairs),
            len(candidates),
            len(published),
            self._last_cycle_summary["best_candidate"],
            json.dumps(rejection_reasons, sort_keys=True),
        )
        return published[0] if published else None

    def evaluate_pair(self, pair: str) -> tuple[GeneratedSignal | None, str]:
        frame_15m = self.fetch_closed_frame(pair, "15m", 260)
        frame_1h = self.fetch_closed_frame(pair, "1h", 260)
        if len(frame_15m) < 210 or len(frame_1h) < 210:
            return None, "INSUFFICIENT_OHLC"

        frame_15m = self.add_indicators(frame_15m)
        frame_1h = self.add_indicators(frame_1h)
        latest = frame_15m.iloc[-1]
        context = frame_1h.iloc[-1]
        regime = self.detect_regime(frame_15m, frame_1h)

        setups = [
            self.trend_pullback_setup(frame_15m, frame_1h, regime),
            self.breakout_setup(frame_15m, frame_1h, regime),
            self.mean_reversion_setup(frame_15m, frame_1h, regime),
        ]
        valid = [setup for setup in setups if setup is not None]
        if not valid:
            return None, f"NO_SETUP_{regime}"
        setup = max(valid, key=lambda item: float(item["score"]))
        if float(setup["score"]) < self.min_signal_score:
            return None, "SCORE_BELOW_MINIMUM"

        entry = float(latest["close"])
        atr = float(latest["atr"])
        stop_distance = self.stop_distance_for_setup(setup, frame_15m, entry, atr)
        stop_loss = entry - stop_distance
        target = entry + (stop_distance * 2.0)
        economics = self.calculate_net_economics(entry, stop_loss, target)

        ceiling = min(entry * 1.03, entry + atr * 3.0)
        increment = max(atr * 0.05, entry * 0.0001)
        while (
            (
                float(economics["net_rr"]) < self.min_net_rr
                or float(economics["net_profit_tp1_eur"]) < self.min_tp1_net_profit_eur
            )
            and target + increment <= ceiling
        ):
            target += increment
            economics = self.calculate_net_economics(entry, stop_loss, target)

        if not bool(economics["tradable"]):
            return None, "NOT_TRADABLE"
        if float(economics["net_profit_tp1_eur"]) < self.min_tp1_net_profit_eur:
            return None, "NET_PROFIT_TOO_LOW"
        if float(economics["net_rr"]) < self.min_net_rr:
            return None, "NET_RR_TOO_LOW"

        signal_time = datetime.now(timezone.utc)
        reference_time = latest["timestamp"].to_pydatetime()
        reasons = list(setup["reasons"])
        reasons.extend(
            [
                f"regime={regime}",
                f"rsi_15m={float(latest['rsi']):.1f}",
                f"ema20_15m={float(latest['ema20']):.8f}",
                f"ema50_15m={float(latest['ema50']):.8f}",
                f"ema200_1h={float(context['ema200']):.8f}",
                f"volume_ratio={float(latest['volume_ratio']):.2f}",
            ]
        )
        score = min(100.0, float(setup["score"]))
        return GeneratedSignal(
            strategy=str(setup["strategy"]),
            pair=pair,
            timeframe="15m",
            regime=regime,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=target,
            technical_take_profit=target,
            effective_take_profit=target,
            signal_time=signal_time,
            reference_candle_time=reference_time,
            entry_timing="ENTER_ON_SIGNAL_RECEIPT",
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
            stop_distance=stop_distance,
            stop_pct=(stop_distance / entry * 100.0),
            atr=atr,
            stop_atr_ratio=(stop_distance / atr if atr > 0 else 0.0),
            tp_atr_ratio=((target - entry) / atr if atr > 0 else 0.0),
            historical_sample_size=0,
            historical_win_rate=None,
            historical_expected_value_eur=None,
            historical_mfe_percentile=0.0,
            historical_mae_percentile=0.0,
            probability_confidence="NOT_USED",
            validation_status="RULES_PASSED",
            validation_reason="; ".join(reasons),
            signal_class="B",
            score=score,
            probability=None,
            reasons=reasons,
            score_breakdown={
                "setup_score": score,
                "rsi": round(float(latest["rsi"]), 2),
                "volume_ratio": round(float(latest["volume_ratio"]), 3),
            },
        ), "OK"

    def fetch_closed_frame(self, pair: str, timeframe: str, limit: int) -> pd.DataFrame:
        rows = self.client.fetch_all(
            """SELECT timestamp, open, high, low, close, volume
            FROM market_data.ohlc
            WHERE exchange = %s AND pair = %s AND timeframe = %s
            ORDER BY timestamp DESC
            LIMIT %s""",
            (self.exchange, pair, timeframe, limit + 2),
        )
        frame = pd.DataFrame(
            rows,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        if frame.empty:
            return frame
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        for column in ["open", "high", "low", "close", "volume"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        cutoff = pd.Timestamp.now(tz="UTC").timestamp()
        seconds = _TIMEFRAME_SECONDS[timeframe]
        candle_end = frame["timestamp"].astype("int64") // 1_000_000_000 + seconds
        frame = frame[candle_end <= cutoff]
        return frame.sort_values("timestamp").tail(limit).reset_index(drop=True)

    @staticmethod
    def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
        data = frame.copy()
        close = data["close"]
        high = data["high"]
        low = data["low"]
        data["ema20"] = close.ewm(span=20, adjust=False).mean()
        data["ema50"] = close.ewm(span=50, adjust=False).mean()
        data["ema200"] = close.ewm(span=200, adjust=False).mean()

        delta = close.diff()
        gains = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        losses = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        rs = gains / losses.replace(0, float("nan"))
        data["rsi"] = (100 - (100 / (1 + rs))).fillna(50.0)

        previous_close = close.shift(1)
        true_range = pd.concat(
            [(high - low), (high - previous_close).abs(), (low - previous_close).abs()],
            axis=1,
        ).max(axis=1)
        data["atr"] = true_range.ewm(alpha=1 / 14, adjust=False).mean()
        data["volume_avg20"] = data["volume"].rolling(20).mean()
        data["volume_ratio"] = (
            data["volume"] / data["volume_avg20"].replace(0, float("nan"))
        ).fillna(0.0)
        middle = close.rolling(20).mean()
        deviation = close.rolling(20).std(ddof=0)
        data["bb_middle"] = middle
        data["bb_upper"] = middle + deviation * 2
        data["bb_lower"] = middle - deviation * 2
        data["bb_width"] = (
            (data["bb_upper"] - data["bb_lower"])
            / middle.replace(0, float("nan"))
        ).fillna(0.0)
        data["high20_prev"] = high.shift(1).rolling(20).max()
        return data

    @staticmethod
    def detect_regime(frame_15m: pd.DataFrame, frame_1h: pd.DataFrame) -> str:
        latest = frame_15m.iloc[-1]
        context = frame_1h.iloc[-1]
        if (
            context["close"] > context["ema200"]
            and context["ema20"] > context["ema50"] > context["ema200"]
        ):
            return "TREND_UP"
        if context["close"] < context["ema200"] and context["ema20"] < context["ema50"]:
            return "TREND_DOWN"
        if float(latest["bb_width"]) < 0.025:
            return "RANGE"
        return "NEUTRAL"

    @staticmethod
    def trend_pullback_setup(
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
    ) -> dict[str, Any] | None:
        del frame_1h
        if regime != "TREND_UP":
            return None
        row = frame_15m.iloc[-1]
        previous = frame_15m.iloc[-2]
        touched_ema = float(row["low"]) <= float(row["ema20"]) * 1.002
        recovered = (
            float(row["close"]) > float(row["ema20"])
            and float(row["close"]) > float(previous["close"])
        )
        if not (
            touched_ema
            and recovered
            and 45 <= float(row["rsi"]) <= 68
            and float(row["volume_ratio"]) >= 0.70
        ):
            return None
        score = 60.0
        score += min(12.0, max(0.0, (float(row["volume_ratio"]) - 0.7) * 15.0))
        score += 10.0 if float(row["ema20"]) > float(row["ema50"]) else 0.0
        score += 8.0 if 50 <= float(row["rsi"]) <= 62 else 3.0
        return {
            "strategy": "Trend Pullback",
            "score": score,
            "reasons": [
                "trend 1h rialzista",
                "pullback EMA20 recuperato",
                "momentum 15m positivo",
            ],
        }

    @staticmethod
    def breakout_setup(
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
    ) -> dict[str, Any] | None:
        if regime == "TREND_DOWN":
            return None
        row = frame_15m.iloc[-1]
        if not (
            float(row["close"]) > float(row["high20_prev"])
            and 52 <= float(row["rsi"]) <= 74
            and float(row["volume_ratio"]) >= 1.15
        ):
            return None
        context = frame_1h.iloc[-1]
        score = 62.0
        score += min(18.0, max(0.0, (float(row["volume_ratio"]) - 1.15) * 20.0))
        score += 10.0 if float(context["close"]) > float(context["ema200"]) else 3.0
        score += 6.0 if float(row["close"]) > float(row["ema20"]) > float(row["ema50"]) else 0.0
        return {
            "strategy": "Breakout 20",
            "score": score,
            "reasons": [
                "rottura massimo 20 candele",
                "volume sopra media",
                "RSI conferma il breakout",
            ],
        }

    @staticmethod
    def mean_reversion_setup(
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
    ) -> dict[str, Any] | None:
        if regime not in {"RANGE", "NEUTRAL"}:
            return None
        row = frame_15m.iloc[-1]
        previous = frame_15m.iloc[-2]
        context = frame_1h.iloc[-1]
        if float(context["close"]) < float(context["ema200"]) * 0.98:
            return None
        reentry = (
            float(previous["close"]) <= float(previous["bb_lower"])
            and float(row["close"]) > float(row["bb_lower"])
        )
        if not (
            reentry
            and 28 <= float(row["rsi"]) <= 46
            and float(row["close"]) > float(previous["close"])
        ):
            return None
        score = 64.0
        score += 10.0 if float(row["rsi"]) < 40 else 5.0
        score += 8.0 if float(row["volume_ratio"]) >= 0.8 else 2.0
        score += 6.0 if regime == "RANGE" else 0.0
        return {
            "strategy": "Mean Reversion Bollinger",
            "score": score,
            "reasons": [
                "rientro nelle Bande di Bollinger",
                "RSI in recupero",
                "assenza di forte trend ribassista 1h",
            ],
        }

    @staticmethod
    def stop_distance_for_setup(
        setup: dict[str, Any],
        frame: pd.DataFrame,
        entry: float,
        atr: float,
    ) -> float:
        strategy = str(setup["strategy"])
        recent_low = float(frame["low"].tail(10).min())
        if strategy == "Breakout 20":
            distance = max(
                atr * 1.25,
                entry - float(frame.iloc[-1]["high20_prev"]) + atr * 0.25,
            )
        elif strategy == "Mean Reversion Bollinger":
            distance = max(atr * 1.10, entry - recent_low + atr * 0.15)
        else:
            distance = max(atr * 1.20, entry - recent_low + atr * 0.10)
        return min(max(distance, entry * 0.003), entry * 0.025)

    def find_active_duplicate(self, signal: GeneratedSignal) -> int | None:
        rows = self.client.fetch_all(
            """SELECT id
            FROM signals.generated_signals
            WHERE pair = %s AND timeframe = %s
              AND status IN ('NEW', 'OPEN')
              AND created_at >= NOW() - (%s * INTERVAL '1 minute')
            ORDER BY created_at DESC
            LIMIT 1""",
            (signal.pair, signal.timeframe, self.signal_cooldown_minutes),
        )
        return int(rows[0][0]) if rows else None

    def publish_daily_signal_report(self) -> bool:
        if not self.enable_daily_signal_report:
            return False
        now = datetime.now(timezone.utc)
        if self._last_no_trade_report_at is not None:
            hours = (now - self._last_no_trade_report_at).total_seconds() / 3600
            if hours < self.daily_signal_report_hours:
                return False
        recent = self.client.fetch_all(
            """SELECT COUNT(*) FROM signals.generated_signals
            WHERE created_at >= NOW() - (%s * INTERVAL '1 hour')""",
            (self.daily_signal_report_hours,),
        )
        recent_count = int(recent[0][0] or 0) if recent else 0
        summary = self._last_cycle_summary or {}
        message = (
            "📊 <b>REPORT SCANNER CRYPTO</b>\n\n"
            f"• Coppie monitorate: <b>{summary.get('pairs_scanned', len(self.pairs))}</b>\n"
            f"• Setup qualificati ultimo ciclo: <b>{summary.get('qualified', 0)}</b>\n"
            f"• Segnali inviati ultimo ciclo: <b>{summary.get('published', 0)}</b>\n"
            f"• Segnali ultime {self.daily_signal_report_hours}h: <b>{recent_count}</b>\n"
            f"• Miglior setup: <b>{summary.get('best_candidate', 'NONE')}</b>\n"
            f"• Motivi senza segnale: <code>{json.dumps(summary.get('rejections', {}), sort_keys=True)}</code>\n\n"
            "Strategie attive: Trend Pullback, Breakout 20, Mean Reversion Bollinger."
        )
        self.event_bus.publish(Event(EventType.REPORT_READY, {"message": message}))
        self._last_no_trade_report_at = now
        return True
