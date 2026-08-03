"""Private daily spot selector: one LONG position or stay in USDT.

Runs once at 06:00 Europe/Rome, ranks liquid crypto assets against BTC and opens
at most one PAPER position with a maximum holding time of 12 hours.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from project.relative_strength_scanner import STRATEGY_NAME as PUBLIC_STRATEGY_NAME
from project.relative_strength_v2_scanner import RelativeStrengthV2Scanner
from project.shared.events import Event, EventType
from project.strategy_engine.service import GeneratedSignal

STRATEGY_NAME = "Private Relative Strength Intraday"
RUNTIME_VERSION = "PRIVATE_RS_SPOT_12H_V1"
ROME = ZoneInfo("Europe/Rome")


class PersonalIntradayScanner(RelativeStrengthV2Scanner):
    """Select one strongest spot asset; never emits SHORT signals."""

    def __init__(
        self,
        *args: Any,
        initial_budget_eur: float = 10.0,
        min_net_target_eur: float = 0.10,
        max_hold_candles: int = 48,
        **kwargs: Any,
    ) -> None:
        kwargs.update(
            strongest_count=3,
            weakest_count=1,
            max_signals_per_day=1,
            max_open_positions=1,
            max_per_pair_day=1,
            max_signals_per_cycle=1,
            max_hold_candles=max_hold_candles,
        )
        super().__init__(*args, **kwargs)
        self.initial_budget_eur = max(1.0, float(initial_budget_eur))
        self.min_net_target_eur = max(0.0, float(min_net_target_eur))
        self.max_hold_candles = max(1, int(max_hold_candles))
        self._last_decision_date: str | None = None

    def _current_budget(self) -> float:
        rows = self.client.fetch_all(
            """SELECT COALESCE(SUM(COALESCE(net_profit_tp1_eur,0)-COALESCE(net_loss_sl_eur,0)),0)
               FROM signals.generated_signals
               WHERE score_breakdown->>'runtime_version' = %s
                 AND closed_at IS NOT NULL""",
            (RUNTIME_VERSION,),
        )
        cumulative = float(rows[0][0] or 0.0) if rows else 0.0
        return max(0.0, self.initial_budget_eur + cumulative)

    def _open_count(self) -> int:
        rows = self.client.fetch_all(
            """SELECT COUNT(*) FROM signals.generated_signals
               WHERE score_breakdown->>'runtime_version' = %s
                 AND status IN ('NEW','OPEN')""",
            (RUNTIME_VERSION,),
        )
        return int(rows[0][0] or 0) if rows else 0

    def _publish_stay_usdt(self, reason: str, universe_size: int = 0) -> None:
        budget = self._current_budget()
        message = (
            "⚪ <b>INVESTIMENTO PRIVATO · RESTA IN USDT</b>\n\n"
            f"Capitale disponibile: <b>€{budget:.2f}</b>\n"
            f"Crypto classificate: <b>{universe_size}</b>\n"
            f"Motivo: <b>{reason}</b>\n\n"
            "Non c'è oggi un setup spot con forza relativa e rendimento netto atteso sufficienti."
        )
        self.event_bus.publish(Event(EventType.REPORT_READY, {
            "message": message,
            "trusted_html": True,
            "private_portfolio": True,
            "decision": "STAY_USDT",
        }))

    def evaluate(self) -> GeneratedSignal | None:
        today = datetime.now(ROME).date().isoformat()
        if self._last_decision_date == today:
            return None
        self._last_decision_date = today

        if self._open_count() > 0:
            self._publish_stay_usdt("posizione personale già aperta")
            return None

        budget = self._current_budget()
        if budget < self.min_notional_eur:
            self._publish_stay_usdt("capitale inferiore al minimo operativo")
            return None
        self.trade_notional_eur = budget

        btc_15m = self._frame("BTC/USD", "15m", 160)
        btc_1h = self._frame("BTC/USD", "1h", 120)
        if len(btc_15m) < 40 or len(btc_1h) < 30:
            self._publish_stay_usdt("dati di mercato insufficienti")
            return None

        ranked: list[dict[str, Any]] = []
        for pair in self.pairs:
            if pair == "BTC/USD":
                continue
            try:
                item = self._rank_pair(pair, btc_15m, btc_1h)
            except Exception:
                self.logger.exception("PRIVATE_RS pair_failed pair=%s", pair)
                continue
            if item is not None:
                ranked.append(item)
        ranked.sort(key=lambda value: float(value["rs_score"]), reverse=True)

        for rank, item in enumerate(ranked[:3], start=1):
            if float(item["rs_score"]) < self.min_abs_score:
                continue
            candidate = self._build_candidate(item, "LONG", rank, len(ranked))
            if candidate is None:
                continue
            if float(candidate.net_profit_tp1_eur) < self.min_net_target_eur:
                continue
            breakdown = dict(candidate.score_breakdown)
            breakdown.update({
                "runtime_version": RUNTIME_VERSION,
                "strategy_version": "PRIVATE_SPOT_12H_V1",
                "private_portfolio": True,
                "initial_budget_eur": self.initial_budget_eur,
                "budget_before_eur": budget,
                "max_hold_candles": self.max_hold_candles,
                "max_hold_hours": 12,
                "public_strategy_reference": PUBLIC_STRATEGY_NAME,
            })
            candidate = replace(
                candidate,
                strategy=STRATEGY_NAME,
                regime="PRIVATE_SPOT_RELATIVE_STRENGTH_LONG",
                score_breakdown=breakdown,
                reasons=[*candidate.reasons, "portafoglio personale spot: uscita obbligatoria entro 12 ore"],
            )
            signal_id = self.save_signal(candidate)
            signal = replace(candidate, signal_id=signal_id)
            self.publish_signal_event(signal)
            self.logger.info(
                "PRIVATE_RS_BUY id=%s pair=%s budget=%.2f net_target=%.4f rank=%s/%s",
                signal_id, signal.pair, budget, signal.net_profit_tp1_eur, rank, len(ranked),
            )
            return signal

        self._publish_stay_usdt("nessuna crypto supera tutti i filtri netti", len(ranked))
        return None
