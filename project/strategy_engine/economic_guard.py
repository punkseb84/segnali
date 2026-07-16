"""Economic, lifecycle and memory guards for probabilistic signals."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from indicators import add_indicators
from project.shared.events import Event, EventType
from project.shared.memory import release_unused_memory
from project.strategy_engine.probabilistic_service import ProbabilisticStrategyEngine as _ProbabilisticStrategyEngine
from project.strategy_engine.service import GeneratedSignal, StrategyEngine


class EconomicallyGuardedProbabilisticStrategyEngine(_ProbabilisticStrategyEngine):
    """Keep probabilistic scoring while separating watchlists and limiting RSS."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.logger.info(
            "Watchlist isolation enabled operative_classes=%s watchlist_classes=%s",
            self.operative_signal_classes,
            self.watchlist_signal_classes,
        )
        self.logger.info("Strategy live-frame cache enabled")

    def evaluate(self) -> GeneratedSignal | None:
        try:
            return super().evaluate()
        finally:
            rss_mb = release_unused_memory()
            if rss_mb is not None:
                self.logger.info("MEMORY_RELEASE phase=strategy rss_mb=%.1f", rss_mb)

    def fetch_strategy_candidates(self, limit: int = 10) -> list[dict[str, Any]]:
        """Build indicators once per pair/timeframe instead of once per candidate."""
        candidates = StrategyEngine.fetch_strategy_candidates(self, limit)
        price_cache: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
        frame_cache: dict[tuple[str, str], pd.DataFrame | None] = {}
        enriched: list[dict[str, Any]] = []

        for candidate in candidates:
            key = (str(candidate["pair"]), str(candidate["timeframe"]))
            if key not in price_cache:
                price_cache[key] = self.research_repository.fetch_ohlc(
                    key[0], key[1], limit=self.ohlc_limit
                )
            if key not in frame_cache:
                frame_cache[key] = self._build_live_frame(price_cache[key])

            frame = frame_cache[key]
            live_context = (
                self._score_prebuilt_frame(frame, candidate)
                if frame is not None
                else {
                    "score": 0.0,
                    "regime": "INSUFFICIENT_DATA",
                    "breakdown": {"data_quality": 0.0},
                }
            )
            item = dict(candidate)
            item["live_setup_score"] = live_context["score"]
            item["live_setup_breakdown"] = live_context["breakdown"]
            item["live_setup_regime"] = live_context["regime"]
            enriched.append(item)

        self.logger.info(
            "STRATEGY_FRAME_CACHE candidates=%s unique_frames=%s",
            len(candidates),
            len(frame_cache),
        )
        return sorted(
            enriched,
            key=lambda item: (
                float(item.get("live_setup_score") or 0.0),
                float(item.get("profit_factor") or 0.0),
                float(item.get("expectancy") or 0.0),
            ),
            reverse=True,
        )

    @staticmethod
    def _build_live_frame(prices: list[tuple[Any, ...]]) -> pd.DataFrame | None:
        if len(prices) < 60:
            return None
        frame = pd.DataFrame(
            list(reversed(prices)),
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        numeric = ["open", "high", "low", "close", "volume"]
        frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
        return add_indicators(frame).reset_index(drop=True)

    def _score_prebuilt_frame(
        self,
        frame: pd.DataFrame,
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        latest = frame.iloc[-1]
        previous = frame.iloc[-2]
        prior_window = frame.iloc[-21:-1]
        required = [
            "close", "open", "high", "low", "volume", "ema20", "ema50",
            "ema200", "rsi14", "macd_hist", "atr14", "adx14",
            "relative_volume", "support",
        ]
        if prior_window.empty or any(pd.isna(latest.get(name)) for name in required):
            return {
                "score": 0.0,
                "regime": "INDICATORS_NOT_READY",
                "breakdown": {"data_quality": 0.0},
            }

        parameters = dict(candidate.get("parameters") or {})
        rsi_low, rsi_high = self._parse_rsi_range(
            str(parameters.get("rsi_range", "0-100"))
        )
        adx_min = float(parameters.get("adx_min", 0.0) or 0.0)
        relative_volume_min = float(
            parameters.get("relative_volume_min", 0.0) or 0.0
        )
        requested_regime = str(parameters.get("market_regime", "ANY"))

        close = float(latest["close"])
        rsi = float(latest["rsi14"])
        adx_value = float(latest["adx14"])
        relative_volume = float(latest["relative_volume"])
        prior_high = float(prior_window["high"].max())
        prior_low = float(prior_window["low"].min())
        actual_regime = self._infer_regime(frame, prior_high)

        breakdown: dict[str, float] = {"data_quality": 5.0}
        if rsi_low <= rsi <= rsi_high:
            breakdown["rsi_fit"] = 15.0
        else:
            distance = min(abs(rsi - rsi_low), abs(rsi - rsi_high))
            breakdown["rsi_fit"] = round(
                max(0.0, 8.0 * (1.0 - distance / 20.0)), 4
            )

        breakdown["adx_strength"] = 10.0 if adx_min <= 0 else round(
            min(10.0, max(0.0, adx_value / adx_min * 10.0)), 4
        )
        breakdown["relative_volume"] = 10.0 if relative_volume_min <= 0 else round(
            min(10.0, max(0.0, relative_volume / relative_volume_min * 10.0)), 4
        )

        trend_points = 0.0
        if close > float(latest["ema200"]):
            trend_points += 5.0
        if float(latest["ema20"]) > float(latest["ema50"]):
            trend_points += 5.0
        breakdown["trend"] = trend_points

        momentum_points = 0.0
        macd_now = float(latest["macd_hist"])
        if macd_now > 0:
            momentum_points += 5.0
        if macd_now > float(previous["macd_hist"]):
            momentum_points += 5.0
        breakdown["momentum"] = momentum_points
        breakdown["regime_fit"] = self._regime_points(
            requested_regime, actual_regime
        )
        breakdown["pattern"] = self._pattern_points(
            str(candidate.get("strategy") or ""),
            frame,
            prior_high,
            prior_low,
        )
        return {
            "score": round(min(100.0, sum(breakdown.values())), 4),
            "regime": actual_regime,
            "breakdown": breakdown,
        }

    def classify_signal(
        self,
        best: dict[str, Any],
        economics: dict[str, float | bool],
        validation: dict[str, str],
        probability_context: dict[str, Any],
        regime_aligned: bool,
        ev_decision: dict[str, Any] | None = None,
        score: float = 0.0,
    ) -> str:
        signal_class = super().classify_signal(
            best, economics, validation, probability_context,
            regime_aligned, ev_decision, score,
        )
        if signal_class not in self.operative_signal_classes:
            return signal_class

        net_profit = float(economics.get("net_profit_tp1_eur") or 0.0)
        net_rr = float(economics.get("net_rr") or 0.0)
        if net_profit >= self.min_tp1_net_profit_eur and net_rr >= self.min_net_rr:
            return signal_class

        self.logger.info(
            "SIGNAL_DOWNGRADED class_from=%s class_to=C reason=economic_minimums "
            "net_profit=%.6f min_net_profit=%.6f net_rr=%.4f min_net_rr=%.4f",
            signal_class, net_profit, self.min_tp1_net_profit_eur,
            net_rr, self.min_net_rr,
        )
        return "C"

    def find_active_duplicate(self, signal: GeneratedSignal) -> int | None:
        common_params = (
            signal.strategy, signal.pair, signal.timeframe, signal.regime,
        )
        if signal.signal_class in self.watchlist_signal_classes:
            rows = self.research_repository.client.fetch_all(
                """SELECT id
                FROM signals.generated_signals
                WHERE strategy = %s AND pair = %s AND timeframe = %s AND regime = %s
                  AND signal_class = %s
                  AND status = 'WATCHLIST'
                  AND created_at >= NOW() - (%s * INTERVAL '1 minute')
                ORDER BY created_at DESC
                LIMIT 1""",
                (*common_params, signal.signal_class, self.signal_cooldown_minutes),
            )
        else:
            operative_classes = tuple(self.operative_signal_classes) or ("A", "B")
            placeholders = ", ".join(["%s"] * len(operative_classes))
            rows = self.research_repository.client.fetch_all(
                f"""SELECT id
                FROM signals.generated_signals
                WHERE strategy = %s AND pair = %s AND timeframe = %s AND regime = %s
                  AND signal_class IN ({placeholders})
                  AND status IN ('NEW', 'OPEN')
                  AND created_at >= NOW() - (%s * INTERVAL '1 minute')
                ORDER BY created_at DESC
                LIMIT 1""",
                (*common_params, *operative_classes, self.signal_cooldown_minutes),
            )
        return int(rows[0][0]) if rows else None

    def save_signal(self, signal: GeneratedSignal) -> int:
        signal_id = super().save_signal(signal)
        if signal.signal_class in self.watchlist_signal_classes:
            self.research_repository.client.execute(
                """UPDATE signals.generated_signals
                SET status = 'WATCHLIST'
                WHERE id = %s""",
                (signal_id,),
            )
            self.logger.info(
                "WATCHLIST_STATUS_SET id=%s class=%s pair=%s strategy=%s",
                signal_id, signal.signal_class, signal.pair, signal.strategy,
            )
        return signal_id

    def publish_daily_signal_report(self) -> bool:
        if not self.enable_daily_signal_report:
            return False
        now = datetime.now(timezone.utc)
        if self._last_no_trade_report_at is not None:
            elapsed_hours = (now - self._last_no_trade_report_at).total_seconds() / 3600.0
            if elapsed_hours < self.daily_signal_report_hours:
                return False

        operative_classes = tuple(self.operative_signal_classes) or ("A", "B")
        placeholders = ", ".join(["%s"] * len(operative_classes))
        recent = self.research_repository.client.fetch_all(
            f"""SELECT COUNT(*)
            FROM signals.generated_signals
            WHERE created_at >= NOW() - (%s * INTERVAL '1 hour')
              AND signal_class IN ({placeholders})""",
            (self.daily_signal_report_hours, *operative_classes),
        )
        recent_count = int(recent[0][0] or 0) if recent else 0
        if recent_count > 0:
            return False

        cycle_summary = self._last_cycle_summary or {}
        top_rejections = cycle_summary.get("top_rejections") or {}
        diagnostics = cycle_summary.get("diagnostics") or {}
        message = (
            "📊 Daily signal report\n"
            f"Nessun segnale operativo nelle ultime {self.daily_signal_report_hours}h.\n"
            f"Ultimo ciclo Strategy Engine: candidati={cycle_summary.get('candidates', 0)} "
            f"A={cycle_summary.get('class_a', 0)} B={cycle_summary.get('class_b', 0)} "
            f"C={cycle_summary.get('class_c', 0)} rejected={cycle_summary.get('rejected', 0)}.\n"
            f"Miglior candidato: {cycle_summary.get('best_candidate', 'NONE')}.\n"
            f"Top motivi di rifiuto: {json.dumps(top_rejections, sort_keys=True) if top_rejections else '{}'}.\n"
            f"Diagnostica ricerca: {json.dumps(diagnostics, sort_keys=True) if diagnostics else '{}'}.\n"
            f"Classi abilitate: operative={','.join(self.operative_signal_classes)} "
            f"watchlist={','.join(self.watchlist_signal_classes)}.\n"
            "Questo non è un segnale di ingresso: evita operazioni forzate e valuta il sistema su molte operazioni."
        )
        self.event_bus.publish(Event(EventType.REPORT_READY, {"message": message}))
        self._last_no_trade_report_at = now
        self.logger.info(
            "STRATEGY daily no-signal report published hours=%s operative_only=true",
            self.daily_signal_report_hours,
        )
        return True
