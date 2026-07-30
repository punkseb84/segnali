"""Run multiple PAPER position monitors without mixing their scanner logic."""
from __future__ import annotations

from typing import Any


class CompositePositionMonitor:
    def __init__(self, *monitors: Any) -> None:
        self.monitors = [monitor for monitor in monitors if monitor is not None]

    def monitor_open_signals(self) -> int:
        return sum(int(monitor.monitor_open_signals() or 0) for monitor in self.monitors)
