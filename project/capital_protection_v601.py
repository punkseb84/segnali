"""V6.0.1 capital-protection fixes without resetting the V6 shadow sample."""
from __future__ import annotations

from typing import Any

from project.capital_protection_report import CapitalProtectionReportScanner
from project.strategy_engine.service import GeneratedSignal


class CapitalProtectionReportScannerV601(CapitalProtectionReportScanner):
    """Apply the win-rate gate only when it is statistically meaningful."""

    def shadow_win_rate_gate(self, stats: dict[str, Any]) -> dict[str, Any]:
        completed = int(stats.get("completed", 0) or 0)
        wins = int(stats.get("wins", 0) or 0)
        losses = int(stats.get("losses", 0) or 0)

        if completed < self.shadow_min_samples:
            return {
                "evaluated": False,
                "passed": False,
                "status": "INSUFFICIENT_SAMPLE",
                "required_win_rate": None,
            }
        if wins == 0 or losses == 0:
            return {
                "evaluated": False,
                "passed": False,
                "status": "SINGLE_OUTCOME_SAMPLE",
                "required_win_rate": None,
            }

        required = min(
            1.0,
            max(
                0.0,
                float(stats.get("break_even_rate", 0.0) or 0.0)
                + self.shadow_min_win_margin,
            ),
        )
        actual = float(stats.get("win_rate", 0.0) or 0.0)
        return {
            "evaluated": True,
            "passed": actual >= required,
            "status": "PASSED" if actual >= required else "FAILED",
            "required_win_rate": required,
        }

    def admission_decision(self, signal: GeneratedSignal) -> dict[str, Any]:
        decision = dict(super().admission_decision(signal))
        blockers = [
            str(blocker)
            for blocker in (decision.get("blockers") or [])
            if not str(blocker).startswith("win rate ")
        ]
        stats = dict(decision.get("stats") or {})
        gate = self.shadow_win_rate_gate(stats)
        if gate["evaluated"] and not gate["passed"]:
            required = float(gate["required_win_rate"] or 0.0)
            blockers.append(
                f"win rate {float(stats.get('win_rate', 0.0)) * 100:.1f}% "
                f"< soglia {required * 100:.1f}%"
            )

        decision.update(
            {
                "admitted": not blockers,
                "blockers": blockers,
                "win_rate_gate": gate,
            }
        )
        key = (signal.strategy, signal.regime, signal.pair)
        self._admission_cache[key] = decision
        return decision
