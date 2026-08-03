"""Lifecycle monitor for the private 10 EUR spot portfolio."""
from __future__ import annotations

import json
from typing import Any

from project.personal_intraday_scanner import RUNTIME_VERSION
from project.relative_strength_monitor import RelativeStrengthMonitor


class PersonalIntradayMonitor(RelativeStrengthMonitor):
    """Reuse net-cost accounting while isolating private portfolio records."""

    def __init__(self, *args: Any, initial_budget_eur: float = 10.0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.initial_budget = max(1.0, float(initial_budget_eur))

    def _open_signals(self) -> list[dict[str, Any]]:
        rows = self.postgres.fetch_all(
            """SELECT id, strategy, pair, timeframe, entry, stop_loss, take_profit,
                      reference_candle_time, created_at, telegram_sent_at,
                      COALESCE(exchange, 'Kraken'), quantity,
                      COALESCE(estimated_buy_fee_eur, 0),
                      COALESCE(score_breakdown, '{}'::jsonb)
               FROM signals.generated_signals
               WHERE status IN ('NEW', 'OPEN')
                 AND telegram_sent_at IS NOT NULL
                 AND score_breakdown->>'runtime_version' = %s
               ORDER BY created_at ASC LIMIT %s""",
            (RUNTIME_VERSION, self.max_signals_per_cycle),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            state = row[13]
            if isinstance(state, str):
                try:
                    state = json.loads(state)
                except json.JSONDecodeError:
                    state = {}
            result.append({
                "id": int(row[0]), "strategy": str(row[1]), "pair": str(row[2]),
                "timeframe": str(row[3]), "entry": float(row[4]),
                "stop_loss": float(row[5]), "take_profit": float(row[6]),
                "reference_candle_time": row[7], "created_at": row[8],
                "telegram_sent_at": row[9], "exchange": str(row[10]),
                "quantity": float(row[11] or 0.0),
                "estimated_buy_fee_eur": float(row[12] or 0.0),
                "state": dict(state or {}),
            })
        return result

    def _budget_before(self) -> float:
        rows = self.postgres.fetch_all(
            """SELECT COALESCE(SUM(COALESCE(net_profit_tp1_eur,0)-COALESCE(net_loss_sl_eur,0)),0)
               FROM signals.generated_signals
               WHERE score_breakdown->>'runtime_version' = %s
                 AND closed_at IS NOT NULL""",
            (RUNTIME_VERSION,),
        )
        cumulative = float(rows[0][0] or 0.0) if rows else 0.0
        return self.initial_budget + cumulative

    def _close(self, signal: dict[str, Any], price: float, row: Any, resolution: str) -> bool:
        # Preserve the shared accounting, but use private-specific outcome labels.
        private_resolution = {
            "RELATIVE_STRENGTH_STOP": "PRIVATE_SPOT_STOP",
            "RELATIVE_STRENGTH_TARGET": "PRIVATE_SPOT_TARGET",
            "RELATIVE_STRENGTH_REVERSAL": "PRIVATE_SPOT_RS_REVERSAL",
            "RELATIVE_STRENGTH_TIME_EXIT": "PRIVATE_SPOT_12H_EXIT",
        }.get(resolution, resolution)
        return super()._close(signal, price, row, private_resolution)
