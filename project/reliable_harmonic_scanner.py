"""Reliability layer for the seven-strategy live LONG scanner.

This runtime layer fixes delivery/orphan blockers, timestamp precision assumptions and
cross-version performance contamination without changing the seven strategy definitions.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pandas as pd

from project.harmonic_live_scanner import (
    HARMONIC_STRATEGY,
    LIVE_LONG_STRATEGIES,
    HarmonicLiveStrategyScanner,
)
from project.strategy_engine.service import GeneratedSignal


RUNTIME_VERSION = "HARMONIC_LIVE_V5_1"
_TIMEFRAME_SECONDS = {"15m": 900, "1h": 3600}


class ReliableHarmonicLiveStrategyScanner(HarmonicLiveStrategyScanner):
    """Seven live LONG strategies with delivery-aware exposure and clean metrics."""

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
        frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
        duration = pd.to_timedelta(_TIMEFRAME_SECONDS[timeframe], unit="s")
        now = pd.Timestamp.now(tz="UTC")
        frame = frame[(frame["timestamp"] + duration) <= now]
        return frame.sort_values("timestamp").tail(limit).reset_index(drop=True)

    def fetch_forward_performance(self) -> dict[str, dict[str, Any]]:
        placeholders = ", ".join(["%s"] * len(LIVE_LONG_STRATEGIES))
        rows = self.client.fetch_all(
            f"""SELECT strategy, status, net_profit_tp1_eur, net_loss_sl_eur, closed_at
            FROM signals.generated_signals
            WHERE strategy IN ({placeholders})
              AND score_breakdown->>'runtime_version' = %s
              AND status IN ('TARGET_HIT', 'STOP_LOSS')
              AND created_at >= NOW() - INTERVAL '120 days'
            ORDER BY closed_at ASC, id ASC""",
            (*LIVE_LONG_STRATEGIES, RUNTIME_VERSION),
        )
        grouped: dict[str, list[tuple[Any, ...]]] = {
            name: [] for name in LIVE_LONG_STRATEGIES
        }
        for row in rows:
            grouped.setdefault(str(row[0]), []).append(row)
        return {
            strategy: self.calculate_enhanced_performance(items)
            for strategy, items in grouped.items()
        }

    def technical_target(
        self,
        strategy: str,
        frame: pd.DataFrame,
        entry: float,
        stop_distance: float,
        atr: float,
        target_rr: float,
    ) -> float:
        if strategy != HARMONIC_STRATEGY:
            return super().technical_target(
                strategy, frame, entry, stop_distance, atr, target_rr
            )
        harmonic_target = float(
            (self._building_assessment or {}).get(
                "harmonic_target", entry + stop_distance * target_rr
            )
        )
        ceiling = min(entry * 1.03, entry + atr * 3.0)
        # Never invent a target beyond either the harmonic projection or the technical cap.
        return min(harmonic_target, ceiling)

    def _build_signal_for_assessment(
        self,
        pair: str,
        frame_15m: pd.DataFrame,
        regime: str,
        relative: dict[str, float],
        assessment: dict[str, Any],
    ) -> tuple[GeneratedSignal | None, str, dict[str, Any]]:
        # During the global loss guard the evaluate() method already enforces one signal,
        # score >= 78, net R/R >= 1.25 and net profit >= EUR 0.30. Do not additionally
        # suppress every adaptive setup, otherwise the six original strategies can become
        # effectively silent for twelve hours.
        guard_active = self._loss_guard_active
        self._loss_guard_active = False
        try:
            signal, reason, diagnostic = super()._build_signal_for_assessment(
                pair, frame_15m, regime, relative, assessment
            )
        finally:
            self._loss_guard_active = guard_active
        if signal is None:
            return signal, reason, diagnostic
        breakdown = dict(signal.score_breakdown or {})
        breakdown.update(
            {
                "runtime_version": RUNTIME_VERSION,
                "loss_guard_active": bool(guard_active),
            }
        )
        reasons = list(signal.reasons or [])
        if guard_active:
            reasons.append(
                "modalità prudente: score/RR/profitto rafforzati e massimo un segnale"
            )
        return replace(signal, score_breakdown=breakdown, reasons=reasons), reason, diagnostic

    def fetch_open_exposure_pairs(self) -> list[str]:
        placeholders = ", ".join(["%s"] * len(LIVE_LONG_STRATEGIES))
        rows = self.client.fetch_all(
            f"""SELECT DISTINCT pair
            FROM signals.generated_signals
            WHERE strategy IN ({placeholders})
              AND status IN ('NEW', 'OPEN')
              AND signal_class IN ('A', 'B')
              AND telegram_sent_at IS NOT NULL
              AND created_at >= NOW() - INTERVAL '48 hours'
            ORDER BY pair""",
            LIVE_LONG_STRATEGIES,
        )
        return [str(row[0]) for row in rows]

    def find_active_duplicate(self, signal: GeneratedSignal) -> int | None:
        rows = self.client.fetch_all(
            """SELECT id
            FROM signals.generated_signals
            WHERE strategy = %s AND pair = %s AND timeframe = %s
              AND status IN ('NEW', 'OPEN')
              AND telegram_sent_at IS NOT NULL
              AND created_at >= NOW() - (%s * INTERVAL '1 minute')
            ORDER BY created_at DESC
            LIMIT 1""",
            (
                signal.strategy,
                signal.pair,
                signal.timeframe,
                self.signal_cooldown_minutes,
            ),
        )
        return int(rows[0][0]) if rows else None

    def has_recent_probation_signal(self, strategy: str) -> bool:
        rows = self.client.fetch_all(
            """SELECT COUNT(*)
            FROM signals.generated_signals
            WHERE strategy = %s
              AND score_breakdown->>'runtime_version' = %s
              AND telegram_sent_at IS NOT NULL
              AND created_at >= NOW() - (%s * INTERVAL '1 hour')""",
            (strategy, RUNTIME_VERSION, self.probation_interval_hours),
        )
        return bool(rows and int(rows[0][0] or 0) > 0)

    def expire_stale_signals(self) -> None:
        placeholders = ", ".join(["%s"] * len(LIVE_LONG_STRATEGIES))
        self.client.execute(
            f"""UPDATE signals.generated_signals
            SET status = 'EXPIRED',
                closed_at = COALESCE(closed_at, NOW()),
                outcome_resolution = COALESCE(outcome_resolution, 'MAX_LIFETIME_48H')
            WHERE strategy IN ({placeholders})
              AND status IN ('NEW', 'OPEN')
              AND created_at < NOW() - INTERVAL '48 hours'""",
            LIVE_LONG_STRATEGIES,
        )

    def evaluate(self) -> GeneratedSignal | None:
        self.expire_stale_signals()
        result = super().evaluate()
        self._last_cycle_summary.update(
            {
                "runtime_version": RUNTIME_VERSION,
                "delivery_aware_exposure": True,
                "closed_candle_filter": "DATETIME_SAFE",
            }
        )
        self.logger.info(
            "HARMONIC_LIVE_V5_1 runtime=%s delivery_aware=true closed_candles=datetime_safe",
            RUNTIME_VERSION,
        )
        return result
