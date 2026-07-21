"""Final Railway wiring for the hardened eighth LONG strategy."""
from __future__ import annotations

import project.structure_aware_main as structure_main
from project.tori_trendline_v11 import ToriTrendlineDailyPaperScannerV11

structure_main.ToriTrendlineDailyPaperScanner = ToriTrendlineDailyPaperScannerV11
main = structure_main.main

__all__ = ["main"]
