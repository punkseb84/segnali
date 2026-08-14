"""Compliance wrapper for the daily BUY/SELL PAPER scanner.

- Caps displayed/persisted candidate scores to the documented 0-100 scale.
- Appends the requested educational/minors disclaimer to each daily signal.
"""
from __future__ import annotations

from dataclasses import replace
import re

from project.daily_coinbase_buy_sell_runtime import (
    DailyCoinbaseBuySellRuntime,
    DirectionalCandidate,
)

DISCLAIMER = "Contenuto informativo a solo scopo didattico. Vietato ai minori di 18 anni."


class DailyCoinbaseCappedRuntime(DailyCoinbaseBuySellRuntime):
    def _score_directional_candidate(self, *args, **kwargs) -> DirectionalCandidate | None:
        candidate = super()._score_directional_candidate(*args, **kwargs)
        if candidate is None:
            return None

        capped_score = max(0.0, min(100.0, float(candidate.score)))
        reason = re.sub(
            r"score\s+-?\d+(?:\.\d+)?/100",
            f"score {capped_score:.0f}/100",
            str(candidate.reason),
            flags=re.IGNORECASE,
        )
        return replace(candidate, score=capped_score, reason=reason)

    def _format_directional_signal(self, rank: int, c: DirectionalCandidate) -> str:
        message = super()._format_directional_signal(rank, c)
        return f"{message}\n\n⚠️ <i>{DISCLAIMER}</i>"
