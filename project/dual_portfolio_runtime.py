"""Railway entrypoint for two isolated PAPER engines.

Public engine: corrected Relative Strength V3 with a real EUR 100 portfolio.
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
from project.relative_strength_v3_scanner import (
    RUNTIME_VERSION_V3,
    RelativeStrengthV3Scanner,
)
from project.relative_strength_v3_validation import validate_v3_configuration

PUBLIC_PORTFOLIO_BUDGET_EUR = 100.0
PUBLIC_MAX_OPEN = 4
PUBLIC_PER_TRADE_EUR = PUBLIC_PORTFOLIO_BUDGET_EUR / PUBLIC_MAX_OPEN
PUBLIC_DAILY_LOSS_LIMIT_EUR = 2.0
PUBLIC_MAX_CONSECUTIVE_LOSSES = 3


class PublicRelativeStrengthV3Scanner(RelativeStrengthV3Scanner):
    """Corrected scanner with coherent capital allocation and loss guards."""

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
            int(kwargs.get("max_signals_per_day", 6)),
            6,
        )
        kwargs["max_signals_per_cycle"] = 1
        kwargs["volume_ratio_min"] = max(
            float(kwargs.get("volume_ratio_min", 1.0)),
            1.0,
        )
        kwargs["min_abs_score"] = max(
            float(kwargs.get("min_abs_score", 0.60)),
            0.60,
        )
        super().__init__(*args, **kwargs)

    def _loss_guard_active(self) -> bool:
        rows = self.client.fetch_all(
            """SELECT
                   COALESCE(SUM(COALESCE(net_profit_tp1_eur,0)-COALESCE(net_loss_sl_eur,0)),0)
               FROM signals.generated_signals
               WHERE score_breakdown->>'runtime_version' = %s
                 AND closed_at IS NOT NULL
                 AND (closed_at AT TIME ZONE 'Europe/Rome')::date =
                     (NOW() AT TIME ZONE 'Europe/Rome')::date""",
            (RUNTIME_VERSION_V3,),
        )
        daily_net = float(rows[0][0] or 0.0) if rows else 0.0
        if daily_net <= -PUBLIC_DAILY_LOSS_LIMIT_EUR:
            self.logger.warning("RS_V3_BLOCK daily_loss net=%.4f", daily_net)
            return True

        recent = self.client.fetch_all(
            """SELECT COALESCE(net_profit_tp1_eur,0)-COALESCE(net_loss_sl_eur,0)
               FROM signals.generated_signals
               WHERE score_breakdown->>'runtime_version' = %s
                 AND closed_at IS NOT NULL
               ORDER BY closed_at DESC LIMIT %s""",
            (RUNTIME_VERSION_V3, PUBLIC_MAX_CONSECUTIVE_LOSSES),
        )
        if len(recent) >= PUBLIC_MAX_CONSECUTIVE_LOSSES and all(
            float(row[0] or 0.0) < 0 for row in recent
        ):
            self.logger.warning("RS_V3_BLOCK consecutive_losses=%s", len(recent))
            return True
        return False

    def evaluate(self):
        if self._loss_guard_active():
            return None
        return super().evaluate()


def main() -> None:
    validate_v3_configuration(
        portfolio_budget=PUBLIC_PORTFOLIO_BUDGET_EUR,
        max_open=PUBLIC_MAX_OPEN,
        per_trade=PUBLIC_PER_TRADE_EUR,
    )

    # Parent scanner/monitor methods read these module globals dynamically.
    # Point all queries, cooldowns and lifecycle checks to the V3 namespace.
    scanner_module.RUNTIME_VERSION = RUNTIME_VERSION_V3
    monitor_module.RUNTIME_VERSION = RUNTIME_VERSION_V3
    runtime.RUNTIME_VERSION = RUNTIME_VERSION_V3

    runtime.RelativeStrengthScanner = PublicRelativeStrengthV3Scanner
    runtime.RelativeStrengthMonitor = RelativeStrengthV3Monitor
    runtime.RelativeStrengthScheduler = DualPortfolioScheduler
    runtime.main()


if __name__ == "__main__":
    main()
