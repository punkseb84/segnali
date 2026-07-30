"""TRIX + ADX multi-timeframe LONG scanner for PAPER trading."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from project.simple_strategy_scanner import SimpleStrategyScanner
from project.shared.events import Event, EventType
from project.strategy_engine.service import GeneratedSignal

STRATEGY_NAME = "TRIX ADX Trend"
RUNTIME_VERSION = "TRIX_ADX_TREND_V1"


class TrixAdxScanner(SimpleStrategyScanner):
    """Select 1h TRIX momentum entries only inside a confirmed 4h trend."""

    def __init__(
        self,
        *args: Any,
        trix_length: int = 20,
        trix_signal_length: int = 9,
        adx_length: int = 14,
        adx_threshold: float = 25.0,
        volume_ratio_min: float = 1.0,
        max_extension_atr: float = 1.0,
        min_stop_atr: float = 1.0,
        max_stop_atr: float = 1.8,
        structure_lookback: int = 10,
        structure_buffer_atr: float = 0.15,
        max_signals_per_day: int = 2,
        max_open_positions: int = 2,
        max_daily_full_stops: int = 2,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.trix_length = max(2, int(trix_length))
        self.trix_signal_length = max(2, int(trix_signal_length))
        self.adx_length = max(2, int(adx_length))
        self.adx_threshold = max(1.0, float(adx_threshold))
        self.volume_ratio_min = max(0.1, float(volume_ratio_min))
        self.max_extension_atr = max(0.1, float(max_extension_atr))
        self.min_stop_atr = max(0.5, float(min_stop_atr))
        self.max_stop_atr = max(self.min_stop_atr + 0.1, float(max_stop_atr))
        self.structure_lookback = max(3, int(structure_lookback))
        self.structure_buffer_atr = max(0.0, float(structure_buffer_atr))
        self.max_signals_per_day = max(1, int(max_signals_per_day))
        self.max_open_positions = max(1, int(max_open_positions))
        self.max_daily_full_stops = max(1, int(max_daily_full_stops))
        self.logger = __import__(
            "project.shared.logging", fromlist=["get_module_logger"]
        ).get_module_logger("trix-adx-scanner")

    def _runtime_count(self, *, open_only: bool = False, today_only: bool = False) -> int:
        clauses = ["score_breakdown->>'runtime_version' = %s"]
        params: list[Any] = [RUNTIME_VERSION]
        if open_only:
            clauses.append("status IN ('NEW', 'OPEN')")
        if today_only:
            clauses.append(
                "(created_at AT TIME ZONE 'Europe/Rome')::date = "
                "(NOW() AT TIME ZONE 'Europe/Rome')::date"
            )
            clauses.append("telegram_sent_at IS NOT NULL")
        rows = self.client.fetch_all(
            f"""SELECT COUNT(*)
                FROM signals.generated_signals
                WHERE {' AND '.join(clauses)}""",
            tuple(params),
        )
        return int(rows[0][0] or 0) if rows else 0

    def _full_stops_today(self) -> int:
        rows = self.client.fetch_all(
            """SELECT COUNT(*)
               FROM signals.generated_signals
               WHERE score_breakdown->>'runtime_version' = %s
                 AND status = 'STOP_LOSS'
                 AND outcome_resolution = 'TRIX_INITIAL_STOP'
                 AND (closed_at AT TIME ZONE 'Europe/Rome')::date =
                     (NOW() AT TIME ZONE 'Europe/Rome')::date""",
            (RUNTIME_VERSION,),
        )
        return int(rows[0][0] or 0) if rows else 0

    def evaluate(self) -> GeneratedSignal | None:
        sent_today = self._runtime_count(today_only=True)
        open_positions = self._runtime_count(open_only=True)
        full_stops_today = self._full_stops_today()

        if sent_today >= self.max_signals_per_day:
            self._last_cycle_summary = {
                "pairs_scanned": len(self.pairs),
                "qualified": 0,
                "published": 0,
                "signals_today": sent_today,
                "open_positions": open_positions,
                "best_candidate": "DAILY_LIMIT_REACHED",
                "rejections": {"DAILY_LIMIT": len(self.pairs)},
            }
            return None

        if open_positions >= self.max_open_positions:
            self._last_cycle_summary = {
                "pairs_scanned": len(self.pairs),
                "qualified": 0,
                "published": 0,
                "signals_today": sent_today,
                "open_positions": open_positions,
                "best_candidate": "MAX_OPEN_POSITIONS",
                "rejections": {"MAX_OPEN_POSITIONS": len(self.pairs)},
            }
            return None

        if full_stops_today >= self.max_daily_full_stops:
            self._last_cycle_summary = {
                "pairs_scanned": len(self.pairs),
                "qualified": 0,
                "published": 0,
                "signals_today": sent_today,
                "open_positions": open_positions,
                "best_candidate": "DAILY_STOP_CIRCUIT_BREAKER",
                "rejections": {"DAILY_STOP_CIRCUIT_BREAKER": len(self.pairs)},
            }
            return None

        candidates: list[GeneratedSignal] = []
        rejection_reasons: dict[str, int] = {}
        for pair in self.pairs:
            try:
                candidate, reason = self.evaluate_pair(pair)
            except Exception:
                self.logger.exception("TRIX_ADX pair_failed pair=%s", pair)
                candidate, reason = None, "PAIR_ERROR"
            if candidate is not None:
                candidates.append(candidate)
            else:
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

        candidates.sort(key=lambda item: item.score, reverse=True)
        remaining_daily = self.max_signals_per_day - sent_today
        remaining_open = self.max_open_positions - open_positions
        cycle_limit = min(self.max_signals_per_cycle, remaining_daily, remaining_open)

        published: list[GeneratedSignal] = []
        for candidate in candidates:
            if len(published) >= cycle_limit:
                break
            duplicate_id = self.find_active_duplicate(candidate)
            if duplicate_id is not None:
                rejection_reasons["ACTIVE_PAIR_COOLDOWN"] = (
                    rejection_reasons.get("ACTIVE_PAIR_COOLDOWN", 0) + 1
                )
                continue
            signal_id = self.save_signal(candidate)
            signal = replace(candidate, signal_id=signal_id)
            self.publish_signal_event(signal)
            published.append(signal)
            self.logger.info(
                "TRIX_ADX_SIGNAL_SENT id=%s pair=%s score=%.1f entry=%.8f stop=%.8f tp1=%.8f",
                signal_id,
                signal.pair,
                signal.score,
                signal.entry,
                signal.stop_loss,
                signal.take_profit,
            )

        best = candidates[0] if candidates else None
        self._last_cycle_summary = {
            "pairs_scanned": len(self.pairs),
            "qualified": len(candidates),
            "published": len(published),
            "signals_today": sent_today + len(published),
            "open_positions": open_positions + len(published),
            "best_candidate": (
                f"{best.pair} score={best.score:.1f}" if best else "NONE"
            ),
            "rejections": rejection_reasons,
        }
        self.logger.info(
            "TRIX_ADX_SUMMARY pairs=%s qualified=%s published=%s today=%s open=%s best=%s rejections=%s",
            len(self.pairs),
            len(candidates),
            len(published),
            self._last_cycle_summary["signals_today"],
            self._last_cycle_summary["open_positions"],
            self._last_cycle_summary["best_candidate"],
            json.dumps(rejection_reasons, sort_keys=True),
        )
        return published[0] if published else None

    def evaluate_pair(self, pair: str) -> tuple[GeneratedSignal | None, str]:
        frame_1h = self.fetch_closed_frame(pair, "1h", 320)
        frame_4h = self.fetch_closed_frame(pair, "4h", 320)
        if len(frame_1h) < 80 or len(frame_4h) < 220:
            return None, "INSUFFICIENT_OHLC"

        data_1h = self.add_entry_indicators(frame_1h)
        data_4h = self.add_context_indicators(frame_4h)
        latest = data_1h.iloc[-1]
        previous = data_1h.iloc[-2]
        context = data_4h.iloc[-1]
        context_previous = data_4h.iloc[-2]

        required = [
            latest["close"],
            latest["atr"],
            latest["ema20"],
            latest["trix"],
            latest["trix_signal"],
            latest["volume_ratio"],
            previous["trix"],
            context["ema50"],
            context["ema200"],
            context["macd"],
            context["macd_signal"],
            context["adx"],
            context_previous["adx"],
        ]
        if any(pd.isna(value) for value in required):
            return None, "INDICATORS_NOT_READY"

        close_4h = float(context["close"])
        ema50_4h = float(context["ema50"])
        ema200_4h = float(context["ema200"])
        macd_4h = float(context["macd"])
        macd_signal_4h = float(context["macd_signal"])
        adx_4h = float(context["adx"])
        adx_previous = float(context_previous["adx"])

        if not (close_4h > ema50_4h > ema200_4h):
            return None, "4H_TREND_NOT_BULLISH"
        if not (macd_4h > macd_signal_4h and macd_4h > 0):
            return None, "4H_MACD_NOT_POSITIVE"
        if adx_4h < self.adx_threshold:
            return None, "4H_ADX_TOO_LOW"
        if adx_4h <= adx_previous:
            return None, "4H_ADX_NOT_RISING"

        trix_now = float(latest["trix"])
        trix_previous = float(previous["trix"])
        trix_signal_now = float(latest["trix_signal"])
        if not (trix_previous <= 0 < trix_now):
            return None, "1H_TRIX_NO_ZERO_CROSS"
        if trix_now <= trix_signal_now:
            return None, "1H_TRIX_BELOW_SIGNAL"

        volume_ratio = float(latest["volume_ratio"])
        if volume_ratio < self.volume_ratio_min:
            return None, "1H_VOLUME_TOO_LOW"

        entry = float(latest["close"])
        atr = float(latest["atr"])
        ema20_1h = float(latest["ema20"])
        if atr <= 0:
            return None, "INVALID_ATR"
        if entry <= ema20_1h:
            return None, "1H_PRICE_BELOW_EMA20"
        extension_atr = (entry - ema20_1h) / atr
        if extension_atr > self.max_extension_atr:
            return None, "1H_PRICE_TOO_EXTENDED"

        recent_low = float(
            data_1h["low"].iloc[-self.structure_lookback - 1 : -1].min()
        )
        structural_stop = recent_low - (atr * self.structure_buffer_atr)
        structural_distance = entry - structural_stop
        stop_distance = max(structural_distance, atr * self.min_stop_atr)
        if stop_distance > atr * self.max_stop_atr:
            return None, "STRUCTURAL_STOP_TOO_WIDE"
        if stop_distance <= 0:
            return None, "INVALID_STOP"

        stop_loss = entry - stop_distance
        tp1_price = entry + (2.0 * stop_distance)
        economics = self.calculate_net_economics(entry, stop_loss, tp1_price)
        if not bool(economics.get("tradable")):
            return None, "NOT_TRADABLE"
        if float(economics["net_profit_tp1_eur"]) <= 0:
            return None, "TP1_NOT_NET_POSITIVE"

        adx_score = min(20.0, 12.0 + max(0.0, adx_4h - self.adx_threshold) * 0.6)
        macd_denominator = max(abs(ema50_4h), 1e-9)
        macd_strength = max(0.0, (macd_4h - macd_signal_4h) / macd_denominator)
        macd_score = min(15.0, 10.0 + macd_strength * 5000.0)
        trix_score = min(20.0, 12.0 + max(0.0, trix_now) * 1500.0)
        volume_score = min(15.0, 8.0 + max(0.0, volume_ratio - 1.0) * 10.0)
        extension_score = max(0.0, 10.0 * (1.0 - extension_atr / self.max_extension_atr))
        structure_score = max(
            0.0,
            20.0
            - abs((stop_distance / atr) - 1.3) * 10.0,
        )
        score = min(
            100.0,
            adx_score
            + macd_score
            + trix_score
            + volume_score
            + extension_score
            + structure_score,
        )
        if score < self.min_signal_score:
            return None, "SCORE_BELOW_MINIMUM"

        signal_time = datetime.now(timezone.utc)
        reference_time = pd.Timestamp(latest["timestamp"]).to_pydatetime()
        reasons = [
            "EMA50 4h sopra EMA200 e prezzo sopra EMA50",
            "MACD 4h positivo e sopra la signal line",
            f"ADX 4h {adx_4h:.1f} sopra {self.adx_threshold:.1f} e crescente",
            "TRIX 20 su 1h attraversa lo zero al rialzo",
            f"volume 1h {volume_ratio:.2f}x mediana 20",
            f"prezzo a {extension_atr:.2f} ATR dalla EMA20",
            f"stop strutturale a {stop_distance / atr:.2f} ATR",
        ]
        breakdown: dict[str, Any] = {
            "runtime_version": RUNTIME_VERSION,
            "strategy_version": "MODEL_A_V1",
            "execution_mode": "PAPER_DAILY",
            "execution_timeframe": "1h",
            "context_timeframe": "4h",
            "atr": round(atr, 10),
            "risk_distance": round(stop_distance, 10),
            "initial_stop": round(stop_loss, 10),
            "be_trigger_price": round(entry + stop_distance, 10),
            "tp1_price": round(tp1_price, 10),
            "tp1_fraction": 0.5,
            "trailing_atr_multiple": 2.5,
            "trix_length": self.trix_length,
            "trix": round(trix_now, 8),
            "trix_signal": round(trix_signal_now, 8),
            "ema20_1h": round(ema20_1h, 10),
            "ema50_4h": round(ema50_4h, 10),
            "ema200_4h": round(ema200_4h, 10),
            "macd_4h": round(macd_4h, 10),
            "macd_signal_4h": round(macd_signal_4h, 10),
            "adx_4h": round(adx_4h, 4),
            "volume_ratio": round(volume_ratio, 4),
            "extension_atr": round(extension_atr, 4),
            "paper_only": True,
        }
        return GeneratedSignal(
            strategy=STRATEGY_NAME,
            pair=pair,
            timeframe="1h",
            regime="4H_TREND_1H_TRIX",
            entry=entry,
            stop_loss=stop_loss,
            take_profit=tp1_price,
            technical_take_profit=tp1_price,
            effective_take_profit=tp1_price,
            signal_time=signal_time,
            reference_candle_time=reference_time,
            entry_timing="ENTER_AFTER_CLOSED_1H_CANDLE",
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
            stop_pct=stop_distance / entry * 100.0,
            atr=atr,
            stop_atr_ratio=stop_distance / atr,
            tp_atr_ratio=(tp1_price - entry) / atr,
            historical_sample_size=0,
            historical_win_rate=None,
            historical_expected_value_eur=None,
            historical_mfe_percentile=0.0,
            historical_mae_percentile=0.0,
            probability_confidence="FORWARD_TEST",
            validation_status="TRIX_ADX_RULES_PASSED",
            validation_reason="; ".join(reasons),
            signal_class="B",
            score=score,
            probability=None,
            reasons=reasons,
            score_breakdown=breakdown,  # type: ignore[arg-type]
        ), "OK"

    def fetch_closed_frame(self, pair: str, timeframe: str, limit: int) -> pd.DataFrame:
        seconds_map = {"1h": 3600, "4h": 14400}
        rows = self.client.fetch_all(
            """SELECT timestamp, open, high, low, close, volume
               FROM market_data.ohlc
               WHERE LOWER(TRIM(exchange)) = LOWER(TRIM(%s))
                 AND pair = %s AND timeframe = %s
               ORDER BY timestamp DESC
               LIMIT %s""",
            (self.exchange, pair, timeframe, limit + 4),
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
        candle_end = (
            frame["timestamp"].astype("int64") // 1_000_000_000
            + seconds_map[timeframe]
        )
        frame = frame[candle_end <= cutoff]
        return frame.sort_values("timestamp").tail(limit).reset_index(drop=True)

    def add_entry_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        data = frame.copy()
        close = data["close"].astype(float)
        high = data["high"].astype(float)
        low = data["low"].astype(float)
        data["ema20"] = close.ewm(span=20, adjust=False).mean()
        previous_close = close.shift(1)
        true_range = pd.concat(
            [
                high - low,
                (high - previous_close).abs(),
                (low - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        data["atr"] = true_range.ewm(alpha=1 / 14, adjust=False).mean()
        ema1 = close.ewm(span=self.trix_length, adjust=False).mean()
        ema2 = ema1.ewm(span=self.trix_length, adjust=False).mean()
        ema3 = ema2.ewm(span=self.trix_length, adjust=False).mean()
        data["trix"] = ema3.pct_change() * 100.0
        data["trix_signal"] = data["trix"].ewm(
            span=self.trix_signal_length,
            adjust=False,
        ).mean()
        data["volume_median20"] = data["volume"].rolling(20).median()
        data["volume_ratio"] = (
            data["volume"]
            / data["volume_median20"].replace(0, float("nan"))
        )
        return data

    def add_context_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        data = frame.copy()
        close = data["close"].astype(float)
        high = data["high"].astype(float)
        low = data["low"].astype(float)
        data["ema50"] = close.ewm(span=50, adjust=False).mean()
        data["ema200"] = close.ewm(span=200, adjust=False).mean()
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        data["macd"] = ema12 - ema26
        data["macd_signal"] = data["macd"].ewm(span=9, adjust=False).mean()
        data["adx"] = self._adx(high, low, close, self.adx_length)
        return data

    @staticmethod
    def _adx(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        length: int,
    ) -> pd.Series:
        previous_close = close.shift(1)
        true_range = pd.concat(
            [
                high - low,
                (high - previous_close).abs(),
                (low - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        atr = true_range.ewm(alpha=1 / length, adjust=False).mean()
        plus_di = 100.0 * plus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr.replace(0, float("nan"))
        minus_di = 100.0 * minus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr.replace(0, float("nan"))
        denominator = (plus_di + minus_di).replace(0, float("nan"))
        dx = 100.0 * (plus_di - minus_di).abs() / denominator
        return dx.ewm(alpha=1 / length, adjust=False).mean()

    def publish_daily_signal_report(self) -> bool:
        if not self.enable_daily_signal_report:
            return False
        now = datetime.now(timezone.utc)
        if self._last_no_trade_report_at is not None:
            elapsed = (now - self._last_no_trade_report_at).total_seconds() / 3600.0
            if elapsed < self.daily_signal_report_hours:
                return False
        summary = self._last_cycle_summary or {}
        message = (
            "📊 <b>REPORT TRIX + ADX · PAPER</b>\n"
            f"Monitorate: <b>{summary.get('pairs_scanned', len(self.pairs))}</b> · "
            f"Valide: <b>{summary.get('qualified', 0)}</b> · "
            f"Inviate: <b>{summary.get('published', 0)}</b>\n"
            f"Segnali oggi: <b>{summary.get('signals_today', 0)}</b> · "
            f"Posizioni aperte: <b>{summary.get('open_positions', 0)}</b>\n"
            f"Miglior setup: <b>{summary.get('best_candidate', 'NONE')}</b>\n"
            f"Rifiuti ultimo ciclo: <code>{json.dumps(summary.get('rejections', {}), sort_keys=True)}</code>"
        )
        self.event_bus.publish(
            Event(
                EventType.REPORT_READY,
                {"message": message, "trusted_html": True},
            )
        )
        self._last_no_trade_report_at = now
        return True
