"""Railway entrypoint for two isolated PAPER engines.

Public engine: robust-volume Relative Strength V5 with a real EUR 100 portfolio.
Private engine: one spot LONG decision at 06:00 Europe/Rome, EUR 10 budget,
maximum holding time 12 hours, with net costs and explicit USDT exit alerts.
"""
from __future__ import annotations

from typing import Any

import project.relative_strength_main as runtime
import project.relative_strength_monitor as monitor_module
import project.relative_strength_scanner as scanner_module
from project.dual_portfolio_scheduler import DualPortfolioScheduler
from project.relative_strength_v3_monitor import RelativeStrengthV3Monitor
from project.relative_strength_v5_scanner import (
    RUNTIME_VERSION_V5,
    RelativeStrengthV5Scanner,
)
from project.relative_strength_v5_validation import validate_v5_configuration

PUBLIC_PORTFOLIO_BUDGET_EUR = 100.0
PUBLIC_MAX_OPEN = 4
PUBLIC_PER_TRADE_EUR = PUBLIC_PORTFOLIO_BUDGET_EUR / PUBLIC_MAX_OPEN
PUBLIC_DAILY_LOSS_LIMIT_EUR = 2.0
PUBLIC_MAX_CONSECUTIVE_LOSSES = 3


class PublicRelativeStrengthV5Scanner(RelativeStrengthV5Scanner):
    """Robust-volume scanner with coherent allocation and loss guards."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["trade_notional_eur"] = min(
            float(kwargs.get("trade_notional_eur", PUBLIC_PER_TRADE_EUR)),
            PUBLIC_PER_TRADE_EUR,
        )
        kwargs["max_open_positions"] = min(
            int(kwargs.get("max_open_positions", PUBLIC_MAX_OPEN)),
            PUBLIC_MAX_OPEN,
        )
        kwargs["max_signals_per_day"] = min(
            int(kwargs.get("max_signals_per_day", 8)),
            8,
        )
        kwargs["max_signals_per_cycle"] = min(
            int(kwargs.get("max_signals_per_cycle", 2)),
            2,
        )
        # Threshold now refers to robust one-hour volume, not one 15m candle.
        kwargs["volume_ratio_min"] = max(
            0.60,
            float(kwargs.get("volume_ratio_min", 0.60)),
        )
        kwargs["min_abs_score"] = max(
            0.45,
            float(kwargs.get("min_abs_score", 0.45)),
        )
        kwargs["max_extension_atr"] = min(
            1.25,
            float(kwargs.get("max_extension_atr", 1.25)),
        )
        super().__init__(*args, **kwargs)

    def _loss_guard_active(self) -> bool:
        rows = self.client.fetch_all(
            """SELECT COALESCE(SUM(COALESCE(net_profit_tp1_eur,0)-COALESCE(net_loss_sl_eur,0)),0)
               FROM signals.generated_signals
               WHERE score_breakdown->>'runtime_version' = %s
                 AND closed_at IS NOT NULL
                 AND (closed_at AT TIME ZONE 'Europe/Rome')::date =
                     (NOW() AT TIME ZONE 'Europe/Rome')::date""",
            (RUNTIME_VERSION_V5,),
        )
        daily_net = float(rows[0][0] or 0.0) if rows else 0.0
        if daily_net <= -PUBLIC_DAILY_LOSS_LIMIT_EUR:
            self.logger.warning("RS_V5_BLOCK daily_loss net=%.4f", daily_net)
            return True

        recent = self.client.fetch_all(
            """SELECT COALESCE(net_profit_tp1_eur,0)-COALESCE(net_loss_sl_eur,0)
               FROM signals.generated_signals
               WHERE score_breakdown->>'runtime_version' = %s
                 AND closed_at IS NOT NULL
               ORDER BY closed_at DESC LIMIT %s""",
            (RUNTIME_VERSION_V5, PUBLIC_MAX_CONSECUTIVE_LOSSES),
        )
        if len(recent) >= PUBLIC_MAX_CONSECUTIVE_LOSSES and all(
            float(row[0] or 0.0) < 0 for row in recent
        ):
            self.logger.warning("RS_V5_BLOCK consecutive_losses=%s", len(recent))
            return True
        return False

    def evaluate(self):
        if self._loss_guard_active():
            return None
        return super().evaluate()


def main() -> None:
    validate_v5_configuration(
        portfolio_budget=PUBLIC_PORTFOLIO_BUDGET_EUR,
        max_open=PUBLIC_MAX_OPEN,
        per_trade=PUBLIC_PER_TRADE_EUR,
    )

    # Shared lifecycle code reads these module globals dynamically.
    scanner_module.RUNTIME_VERSION = RUNTIME_VERSION_V5
    monitor_module.RUNTIME_VERSION = RUNTIME_VERSION_V5
    runtime.RUNTIME_VERSION = RUNTIME_VERSION_V5
    runtime.RelativeStrengthScanner = PublicRelativeStrengthV5Scanner
    runtime.RelativeStrengthMonitor = RelativeStrengthV3Monitor
    runtime.RelativeStrengthScheduler = DualPortfolioScheduler
    runtime.main()


if __name__ == "__main__":
    main()
