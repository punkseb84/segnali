"""Outcome-aware Ichimoku monitor that classifies dynamic exits as wins or losses."""
from __future__ import annotations

from typing import Any

from project.ichimoku_position_monitor import IchimokuPositionMonitor
from project.shared.events import Event, EventType


class IchimokuOutcomeMonitor(IchimokuPositionMonitor):
    """Persist a cloud exit as a win only when its estimated net result is positive."""

    def _close_signal(
        self,
        signal: dict[str, Any],
        *,
        outcome: str,
        outcome_price: float,
        timestamp: Any,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        resolution: str,
    ) -> bool:
        realized_net = self._realized_net(signal, outcome_price)
        is_dynamic_exit = resolution.startswith("ICHIMOKU_")
        persisted_outcome = outcome
        if is_dynamic_exit:
            persisted_outcome = "TARGET_HIT" if realized_net >= 0 else "STOP_LOSS"

        net_profit = max(realized_net, 0.0)
        net_loss = max(-realized_net, 0.0)
        self.postgres.execute(
            """UPDATE signals.generated_signals
            SET status = %s,
                outcome_price = %s,
                outcome_open_price = %s,
                outcome_high_price = %s,
                outcome_low_price = %s,
                outcome_close_price = %s,
                outcome_candle_time = %s,
                closed_at = NOW(),
                outcome_ambiguous = FALSE,
                outcome_resolution = %s,
                net_profit_tp1_eur = %s,
                net_loss_sl_eur = %s
            WHERE id = %s AND status IN ('NEW', 'OPEN')""",
            (
                persisted_outcome,
                outcome_price,
                open_price,
                high_price,
                low_price,
                close_price,
                timestamp,
                resolution,
                net_profit,
                net_loss,
                signal["id"],
            ),
        )
        event_type = (
            EventType.STOP_LOSS
            if persisted_outcome == "STOP_LOSS"
            else EventType.TARGET_HIT
        )
        payload = {
            **signal,
            "signal_id": signal["id"],
            "outcome": persisted_outcome,
            "outcome_price": outcome_price,
            "outcome_open_price": open_price,
            "outcome_high_price": high_price,
            "outcome_low_price": low_price,
            "close_price": close_price,
            "closed_at": str(timestamp),
            "ambiguous": False,
            "outcome_resolution": resolution,
            "realized_net_eur": realized_net,
            "net_profit_tp1_eur": net_profit,
            "net_loss_sl_eur": net_loss,
        }
        self.event_bus.publish(Event(event_type, payload))
        self.logger.info(
            "ICHIMOKU_POSITION_CLOSED id=%s pair=%s status=%s resolution=%s exit=%.8f net=%.4f",
            signal["id"],
            signal["pair"],
            persisted_outcome,
            resolution,
            outcome_price,
            realized_net,
        )
        return True
