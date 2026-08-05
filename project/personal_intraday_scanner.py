"""Private daily spot selector with transparent ranking and diagnostics."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from project.relative_strength_scanner import RelativeStrengthScanner
from project.relative_strength_v3_scanner import RelativeStrengthV3Scanner
from project.shared.events import Event, EventType
from project.strategy_engine.service import GeneratedSignal

STRATEGY_NAME = "Private Best Opportunity Intraday"
RUNTIME_VERSION = "PRIVATE_BEST_OPPORTUNITY_12H_V2"
ROME = ZoneInfo("Europe/Rome")


class PersonalIntradayScanner(RelativeStrengthV3Scanner):
    def __init__(
        self,
        *args: Any,
        initial_budget_eur: float = 10.0,
        min_net_target_eur: float = 0.05,
        min_quality_score: float = 55.0,
        max_hold_candles: int = 48,
        **kwargs: Any,
    ) -> None:
        kwargs.update(
            strongest_count=8,
            weakest_count=1,
            max_signals_per_day=1,
            max_open_positions=1,
            max_per_pair_day=1,
            max_signals_per_cycle=1,
            max_hold_candles=max_hold_candles,
            min_abs_score=0.20,
            volume_ratio_min=0.60,
            max_extension_atr=1.60,
            target_r=1.50,
        )
        super().__init__(*args, **kwargs)
        self.initial_budget_eur = max(1.0, float(initial_budget_eur))
        self.min_net_target_eur = max(0.0, float(min_net_target_eur))
        self.min_quality_score = max(0.0, float(min_quality_score))
        self.max_hold_candles = max(1, int(max_hold_candles))
        self._last_decision_date: str | None = None

    def _current_budget(self) -> float:
        rows = self.client.fetch_all(
            """SELECT COALESCE(SUM(COALESCE(net_profit_tp1_eur,0)-COALESCE(net_loss_sl_eur,0)),0)
               FROM signals.generated_signals
               WHERE score_breakdown->>'runtime_version' = %s AND closed_at IS NOT NULL""",
            (RUNTIME_VERSION,),
        )
        cumulative = float(rows[0][0] or 0.0) if rows else 0.0
        return max(0.0, self.initial_budget_eur + cumulative)

    def _open_count(self) -> int:
        rows = self.client.fetch_all(
            """SELECT COUNT(*) FROM signals.generated_signals
               WHERE score_breakdown->>'runtime_version' = %s AND status IN ('NEW','OPEN')""",
            (RUNTIME_VERSION,),
        )
        return int(rows[0][0] or 0) if rows else 0

    @staticmethod
    def _quality(item: dict[str, Any]) -> tuple[float, list[str]]:
        frame = item["asset_15m"]
        latest = frame.iloc[-1]
        entry, ema20, atr = float(latest["close"]), float(latest["ema20"]), float(latest["atr"])
        extension = abs(entry - ema20) / atr if atr > 0 else 99.0
        rs = float(item.get("rs_score") or 0.0)
        volume = float(item.get("volume_ratio") or 0.0)
        bullish_candle = entry > float(latest["open"])
        above_ema = entry > ema20
        btc_ok = bool(item.get("btc_bullish"))
        quality = 35.0
        quality += min(25.0, max(0.0, rs) * 18.0)
        quality += min(15.0, max(0.0, volume - 0.5) * 15.0)
        quality += 10.0 if bullish_candle else 0.0
        quality += 10.0 if above_ema else 0.0
        quality += 10.0 if btc_ok else -20.0
        quality += 5.0 if extension <= 1.0 else 0.0
        reasons = []
        if not btc_ok: reasons.append("regime BTC non favorevole")
        if not bullish_candle: reasons.append("ultima candela non rialzista")
        if not above_ema: reasons.append("prezzo sotto EMA20")
        if volume < 0.60: reasons.append(f"volume basso {volume:.2f}x")
        if extension > 1.60: reasons.append(f"estensione elevata {extension:.2f} ATR")
        if rs < 0.20: reasons.append(f"forza relativa debole {rs:+.2f}")
        return max(0.0, min(100.0, quality)), reasons

    def _publish_decision(self, reason: str, ranked: list[dict[str, Any]], diagnostics: list[str]) -> None:
        budget = self._current_budget()
        lines = "\n".join(f"• {line}" for line in diagnostics[:5]) or "• Nessuna candidata con dati completi"
        message = (
            "⚪ <b>INVESTIMENTO PRIVATO · RESTA IN USDT</b>\n\n"
            f"Capitale disponibile: <b>€{budget:.2f}</b>\n"
            f"Crypto classificate: <b>{len(ranked)}</b>\n"
            f"Motivo principale: <b>{reason}</b>\n\n"
            "<b>Migliori candidate analizzate</b>\n"
            f"{lines}\n\n"
            "La decisione non deriva da un filtro nascosto: sono riportati punteggio e motivo di esclusione."
        )
        self.event_bus.publish(Event(EventType.REPORT_READY, {
            "message": message, "trusted_html": True,
            "private_portfolio": True, "decision": "STAY_USDT",
        }))

    def evaluate(self) -> GeneratedSignal | None:
        today = datetime.now(ROME).date().isoformat()
        if self._last_decision_date == today:
            return None
        self._last_decision_date = today
        if self._open_count() > 0:
            self._publish_decision("posizione personale già aperta", [], [])
            return None

        budget = self._current_budget()
        if budget < self.min_notional_eur:
            self._publish_decision("capitale inferiore al minimo operativo", [], [])
            return None
        self.trade_notional_eur = budget

        btc_15m = self._frame("BTC/USD", "15m", 180)
        btc_1h = self._frame("BTC/USD", "1h", 140)
        if len(btc_15m) < 40 or len(btc_1h) < 30:
            self._publish_decision("dati di mercato insufficienti", [], [])
            return None

        ranked: list[dict[str, Any]] = []
        for pair in self.pairs:
            if pair == "BTC/USD":
                continue
            try:
                item = self._rank_pair(pair, btc_15m, btc_1h)
            except Exception:
                self.logger.exception("PRIVATE_V2 pair_failed pair=%s", pair)
                continue
            if item is not None:
                quality, reasons = self._quality(item)
                item["quality_score"] = quality
                item["quality_reasons"] = reasons
                ranked.append(item)
        ranked.sort(key=lambda value: (float(value["quality_score"]), float(value["rs_score"])), reverse=True)

        diagnostics: list[str] = []
        for item in ranked[:5]:
            reasons = item["quality_reasons"]
            diagnostics.append(
                f"<b>{item['pair']}</b>: qualità {float(item['quality_score']):.0f}/100, "
                f"RS {float(item['rs_score']):+.2f}, vol {float(item['volume_ratio']):.2f}x — "
                f"{'idonea' if not reasons else ', '.join(reasons[:2])}"
            )

        for rank, item in enumerate(ranked[:8], start=1):
            quality = float(item["quality_score"])
            if quality < self.min_quality_score or item["quality_reasons"]:
                continue
            candidate = RelativeStrengthScanner._build_candidate(self, item, "LONG", rank, len(ranked))
            if candidate is None or float(candidate.net_profit_tp1_eur) < self.min_net_target_eur:
                continue
            breakdown = dict(candidate.score_breakdown)
            breakdown.update({
                "runtime_version": RUNTIME_VERSION,
                "strategy_version": "PRIVATE_BEST_OPPORTUNITY_V2",
                "private_portfolio": True,
                "quality_score": quality,
                "initial_budget_eur": self.initial_budget_eur,
                "budget_before_eur": budget,
                "max_hold_candles": self.max_hold_candles,
                "max_hold_hours": 12,
            })
            candidate = replace(
                candidate,
                strategy=STRATEGY_NAME,
                regime="PRIVATE_BEST_SPOT_LONG",
                score_breakdown=breakdown,
                reasons=[*candidate.reasons, f"migliore opportunità giornaliera qualità {quality:.0f}/100", "uscita obbligatoria entro 12 ore"],
            )
            signal_id = self.save_signal(candidate)
            signal = replace(candidate, signal_id=signal_id)
            self.publish_signal_event(signal)
            self.logger.info("PRIVATE_V2_BUY id=%s pair=%s quality=%.1f budget=%.2f", signal_id, signal.pair, quality, budget)
            return signal

        best_quality = float(ranked[0]["quality_score"]) if ranked else 0.0
        self._publish_decision(f"migliore qualità {best_quality:.0f}/100, soglia {self.min_quality_score:.0f}/100", ranked, diagnostics)
        return None
