"""Validated live scanner with strict relative-strength momentum rules.

This layer fixes the failure mode exposed by signal #222: a Relative Strength Momentum
setup may no longer be promoted when RSI or MACD confirmation is missing, may not chase
price far above EMA20, and must retain a strategy-specific net R/R of at least 1.45.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from statistics import median
from typing import Any

import pandas as pd

from project.harmonic_live_scanner import LIVE_LONG_STRATEGIES
from project.reliable_harmonic_scanner import RUNTIME_VERSION
from project.strategy_engine.service import GeneratedSignal
from project.version_scoped_harmonic_scanner import VersionScopedHarmonicLiveStrategyScanner


RELATIVE_STRENGTH_STRATEGY = "Relative Strength Momentum"
RELATIVE_STRENGTH_MIN_NET_RR = 1.45


class ValidatedLiveStrategyScanner(VersionScopedHarmonicLiveStrategyScanner):
    """Seven live LONG strategies with strict momentum continuation validation."""

    def fetch_relative_strength_snapshot(self) -> dict[str, dict[str, float]]:
        placeholders = ", ".join(["%s"] * len(self.pairs))
        rows = self.client.fetch_all(
            f"""WITH ranked AS (
                SELECT pair, close,
                       ROW_NUMBER() OVER (PARTITION BY pair ORDER BY timestamp DESC) AS rn
                FROM market_data.ohlc
                WHERE exchange = %s
                  AND timeframe = '15m'
                  AND pair IN ({placeholders})
                  AND timestamp >= NOW() - INTERVAL '3 days'
                  AND timestamp + INTERVAL '15 minutes' <= NOW()
            )
            SELECT pair,
                   MAX(CASE WHEN rn = 1 THEN close END) AS latest_close,
                   MAX(CASE WHEN rn = 5 THEN close END) AS close_1h,
                   MAX(CASE WHEN rn = 9 THEN close END) AS close_2h,
                   MAX(CASE WHEN rn = 17 THEN close END) AS close_4h,
                   MAX(CASE WHEN rn = 97 THEN close END) AS close_24h
            FROM ranked
            WHERE rn IN (1, 5, 9, 17, 97)
            GROUP BY pair""",
            (self.exchange, *self.pairs),
        )
        raw: dict[str, dict[str, float]] = {}
        for pair, latest, close_1h, close_2h, close_4h, close_24h in rows:
            if None in (latest, close_1h, close_2h, close_4h, close_24h):
                continue
            latest_f = float(latest)
            one_f = float(close_1h)
            two_f = float(close_2h)
            four_f = float(close_4h)
            day_f = float(close_24h)
            if min(one_f, two_f, four_f, day_f) <= 0:
                continue
            raw[str(pair)] = {
                "return_1h": latest_f / one_f - 1.0,
                "previous_return_1h": one_f / two_f - 1.0,
                "return_4h": latest_f / four_f - 1.0,
                "return_24h": latest_f / day_f - 1.0,
            }
        if not raw:
            return {}

        median_4h = float(median(item["return_4h"] for item in raw.values()))
        median_24h = float(median(item["return_24h"] for item in raw.values()))
        btc = raw.get("BTC/USD", {})
        btc_return_1h = float(btc.get("return_1h", 0.0))
        btc_previous_return_1h = float(btc.get("previous_return_1h", 0.0))
        btc_return_24h = float(btc.get("return_24h", median_24h))
        ordered = sorted(raw, key=lambda pair: raw[pair]["return_24h"])
        denominator = max(1, len(ordered) - 1)
        for index, pair in enumerate(ordered):
            relative_1h = raw[pair]["return_1h"] - btc_return_1h
            previous_relative_1h = (
                raw[pair]["previous_return_1h"] - btc_previous_return_1h
            )
            raw[pair].update(
                {
                    "median_return_4h": median_4h,
                    "median_return_24h": median_24h,
                    "relative_24h": raw[pair]["return_24h"] - btc_return_24h,
                    "rank_percentile": index / denominator,
                    "relative_1h": relative_1h,
                    "previous_relative_1h": previous_relative_1h,
                    "relative_1h_acceleration": relative_1h - previous_relative_1h,
                    "btc_return_1h": btc_return_1h,
                }
            )
        return raw

    @classmethod
    def relative_strength_assessment(
        cls,
        frame_15m: pd.DataFrame,
        frame_1h: pd.DataFrame,
        regime: str,
        relative: dict[str, float],
    ) -> dict[str, Any]:
        row = frame_15m.iloc[-1]
        previous = frame_15m.iloc[-2]
        context = frame_1h.iloc[-1]
        recent = frame_15m.tail(4)

        leader = (
            float(relative.get("rank_percentile", 0.0)) >= 0.70
            and float(relative.get("relative_24h", 0.0)) >= 0.003
        )
        four_hour_momentum = (
            float(relative.get("return_4h", 0.0))
            >= float(relative.get("median_return_4h", 0.0))
            and float(relative.get("return_4h", 0.0)) > 0.0
        )
        one_hour_leadership = (
            float(relative.get("relative_1h", 0.0)) > 0.0
            and float(relative.get("relative_1h_acceleration", -1.0)) >= 0.0
        )
        btc_safe = float(relative.get("btc_return_1h", 0.0)) >= -0.004
        trend_ok = (
            regime != "TREND_DOWN"
            and float(context["close"]) >= float(context["ema200"]) * 0.99
            and float(context["ema20"]) > float(context["ema50"])
        )
        structure_ok = float(row["close"]) > float(row["ema20"]) >= float(row["ema50"])
        rsi_ok = 48.0 <= float(row["rsi"]) <= 72.0
        macd_improving = float(row["macd_hist"]) > float(previous["macd_hist"])
        volume_ok = float(row["volume_ratio"]) >= 0.70
        not_extended = float(row["close"]) <= float(row["ema20"]) + float(row["atr"]) * 0.80
        pullback_restart = bool(
            (
                recent["low"]
                <= recent["ema20"] + recent["atr"] * 0.25
            ).any()
            and float(row["close"]) > float(previous["close"])
            and float(row["close"]) > float(row["ema20"])
        )

        checks = {
            "leader": leader,
            "four_hour_momentum": four_hour_momentum,
            "one_hour_leadership": one_hour_leadership,
            "btc_safe": btc_safe,
            "trend_ok": trend_ok,
            "structure_ok": structure_ok,
            "rsi_ok": rsi_ok,
            "macd_improving": macd_improving,
            "volume_ok": volume_ok,
            "not_extended": not_extended,
            "pullback_restart": pullback_restart,
        }
        weights = {
            "leader": 20,
            "four_hour_momentum": 12,
            "one_hour_leadership": 12,
            "btc_safe": 4,
            "trend_ok": 14,
            "structure_ok": 10,
            "rsi_ok": 8,
            "macd_improving": 8,
            "volume_ok": 5,
            "not_extended": 4,
            "pullback_restart": 3,
        }
        score = float(sum(weight for name, weight in weights.items() if checks[name]))
        blockers: list[str] = []
        if not leader:
            blockers.append("leadership 24h insufficiente rispetto a BTC e paniere")
        if not four_hour_momentum:
            blockers.append("forza 4h non positiva o non superiore alla mediana")
        if not one_hour_leadership:
            blockers.append("forza relativa 1h non positiva e in accelerazione")
        if not btc_safe:
            blockers.append("BTC in deterioramento oltre -0,40% nell'ultima ora")
        if not trend_ok:
            blockers.append("trend 1h non favorevole")
        if not structure_ok:
            blockers.append("prezzo 15m non sopra EMA20/EMA50")
        if not rsi_ok:
            blockers.append("RSI 15m fuori fascia 48-72")
        if not macd_improving:
            blockers.append("MACD 15m non in miglioramento")
        if not volume_ok:
            blockers.append("volume sotto 0,70 della media")
        if not not_extended:
            blockers.append("ingresso oltre 0,80 ATR sopra EMA20")
        if not pullback_restart:
            blockers.append("manca pullback recente su EMA20 con ripartenza")

        qualified = all(checks.values()) and score >= 80.0
        return cls._assessment(
            RELATIVE_STRENGTH_STRATEGY,
            score,
            qualified,
            [
                "leadership 24h confermata rispetto a BTC e paniere",
                "forza relativa 1h positiva e in accelerazione",
                "RSI e MACD 15m obbligatoriamente confermati",
                "pullback su EMA20 seguito da ripartenza senza inseguire il prezzo",
            ],
            blockers,
            target_rr=2.00,
        )

    def _build_signal_for_assessment(
        self,
        pair: str,
        frame_15m: pd.DataFrame,
        regime: str,
        relative: dict[str, float],
        assessment: dict[str, Any],
    ) -> tuple[GeneratedSignal | None, str, dict[str, Any]]:
        signal, reason, diagnostic = super()._build_signal_for_assessment(
            pair, frame_15m, regime, relative, assessment
        )
        if signal is None or signal.strategy != RELATIVE_STRENGTH_STRATEGY:
            return signal, reason, diagnostic
        if float(signal.net_rr) < RELATIVE_STRENGTH_MIN_NET_RR:
            diagnostic["blockers"] = [
                f"R/R netto Relative Strength sotto soglia {RELATIVE_STRENGTH_MIN_NET_RR:.2f}"
            ]
            diagnostic["net_rr"] = round(float(signal.net_rr), 2)
            return None, "RELATIVE_STRENGTH_NET_RR_TOO_LOW", diagnostic

        breakdown = dict(signal.score_breakdown or {})
        breakdown.update(
            {
                "relative_strength_mode": "STRICT_CONTINUATION",
                "relative_1h_pct": round(float(relative.get("relative_1h", 0.0)) * 100, 3),
                "relative_1h_acceleration_pct": round(
                    float(relative.get("relative_1h_acceleration", 0.0)) * 100, 3
                ),
                "btc_return_1h_pct": round(float(relative.get("btc_return_1h", 0.0)) * 100, 3),
                "strategy_min_net_rr": RELATIVE_STRENGTH_MIN_NET_RR,
            }
        )
        return replace(signal, score_breakdown=breakdown), reason, diagnostic

    def fetch_forward_performance(self) -> dict[str, dict[str, Any]]:
        placeholders = ", ".join(["%s"] * len(LIVE_LONG_STRATEGIES))
        rows = self.client.fetch_all(
            f"""SELECT strategy, status, net_profit_tp1_eur, net_loss_sl_eur, closed_at
            FROM signals.generated_signals
            WHERE strategy IN ({placeholders})
              AND score_breakdown->>'runtime_version' = %s
              AND telegram_sent_at IS NOT NULL
              AND COALESCE(outcome_ambiguous, FALSE) = FALSE
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

    def fetch_loss_streak(self) -> tuple[int, datetime | None]:
        placeholders = ", ".join(["%s"] * len(LIVE_LONG_STRATEGIES))
        rows = self.client.fetch_all(
            f"""SELECT status, closed_at FROM signals.generated_signals
            WHERE strategy IN ({placeholders})
              AND score_breakdown->>'runtime_version' = %s
              AND telegram_sent_at IS NOT NULL
              AND COALESCE(outcome_ambiguous, FALSE) = FALSE
              AND status IN ('TARGET_HIT', 'STOP_LOSS')
              AND closed_at IS NOT NULL
              AND closed_at >= NOW() - INTERVAL '30 days'
            ORDER BY closed_at DESC, id DESC
            LIMIT 12""",
            (*LIVE_LONG_STRATEGIES, RUNTIME_VERSION),
        )
        streak = 0
        last_closed: datetime | None = None
        for status, closed_at in rows:
            if last_closed is None and isinstance(closed_at, datetime):
                last_closed = (
                    closed_at if closed_at.tzinfo else closed_at.replace(tzinfo=timezone.utc)
                )
            if str(status) != "STOP_LOSS":
                break
            streak += 1
        return streak, last_closed

    def save_signal(self, signal: GeneratedSignal) -> int:
        signal_id = super().save_signal(signal)
        self.client.execute(
            "UPDATE signals.generated_signals SET exchange = %s WHERE id = %s",
            (self.exchange, signal_id),
        )
        return signal_id

    def evaluate(self) -> GeneratedSignal | None:
        result = super().evaluate()
        self._last_cycle_summary["relative_strength_mode"] = "STRICT_CONTINUATION"
        self._last_cycle_summary["relative_strength_min_net_rr"] = (
            RELATIVE_STRENGTH_MIN_NET_RR
        )
        self.logger.info(
            "RELATIVE_STRENGTH_VALIDATED runtime=%s min_net_rr=%.2f adaptive_promotion=false",
            RUNTIME_VERSION,
            RELATIVE_STRENGTH_MIN_NET_RR,
        )
        return result
