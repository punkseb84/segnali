"""Fair shadow queue monitoring for Capital Protection V6."""
from __future__ import annotations

from typing import Any

from project.shadow_signal_monitor import ShadowValidationMonitor


class FairShadowValidationMonitor(ShadowValidationMonitor):
    """Use a dedicated, larger shadow queue limit while preserving the live limit."""

    def __init__(
        self,
        *args: Any,
        max_shadow_signals_per_cycle: int = 200,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.max_shadow_signals_per_cycle = max(
            20, int(max_shadow_signals_per_cycle)
        )

    def monitor_shadow_signals(self) -> int:
        live_limit = self.max_signals_per_cycle
        self.max_signals_per_cycle = self.max_shadow_signals_per_cycle
        try:
            return super().monitor_shadow_signals()
        finally:
            self.max_signals_per_cycle = live_limit
