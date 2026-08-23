"""Compliance and reporting wrapper for the daily BUY/SELL PAPER scanner.

- Caps displayed/persisted candidate scores to the documented 0-100 scale.
- Appends the requested educational/minors disclaimer to each daily signal.
- Appends cumulative performance since the BUY/SELL daily strategy started to each nightly recap.
"""
from __future__ import annotations

from dataclasses import replace
from html import escape
import re

from project.daily_coinbase_buy_sell_runtime import (
    DailyCoinbaseBuySellRuntime,
    DirectionalCandidate,
)
from project.daily_coinbase_runtime import DAILY_BUDGET_EUR

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

    def _cumulative_performance(self, through_date) -> str:
        rows = self.postgres.fetch_all(
            """SELECT direction, COALESCE(net_pnl_eur,0), COALESCE(net_return_pct,0),
                      COALESCE(close_reason,status), product_id
               FROM daily_coinbase.trades
               WHERE trade_date <= %s
                 AND status <> 'OPEN'
               ORDER BY trade_date, rank""",
            (through_date,),
        )
        if not rows:
            return "📊 <b>PERFORMANCE DALL’AVVIO</b>\nNessuna operazione chiusa disponibile."

        total = sum(float(r[1] or 0.0) for r in rows)
        wins = sum(1 for r in rows if float(r[1] or 0.0) > 0)
        losses = sum(1 for r in rows if float(r[1] or 0.0) < 0)
        flat = len(rows) - wins - losses
        win_rate = 100.0 * wins / len(rows) if rows else 0.0
        avg_trade = total / len(rows) if rows else 0.0
        buy_pnl = sum(float(r[1] or 0.0) for r in rows if str(r[0]).upper() == 'BUY')
        sell_pnl = sum(float(r[1] or 0.0) for r in rows if str(r[0]).upper() == 'SELL')
        tp_n = sum(1 for r in rows if str(r[3]).upper() == 'TP')
        sl_n = sum(1 for r in rows if str(r[3]).upper() == 'SL')
        midnight_n = sum(1 for r in rows if str(r[3]).upper() == 'MIDNIGHT')

        pair_pnl: dict[str, float] = {}
        for _, pnl, _, _, product_id in rows:
            pair = str(product_id)
            pair_pnl[pair] = pair_pnl.get(pair, 0.0) + float(pnl or 0.0)
        best_pair, best_pair_pnl = max(pair_pnl.items(), key=lambda item: item[1])
        worst_pair, worst_pair_pnl = min(pair_pnl.items(), key=lambda item: item[1])

        return (
            "📊 <b>PERFORMANCE DALL’AVVIO</b>\n"
            f"Trade chiusi: <b>{len(rows)}</b> · positivi {wins} · negativi {losses} · flat {flat}\n"
            f"Win rate: <b>{win_rate:.1f}%</b> · TP {tp_n} · SL {sl_n} · MIDNIGHT {midnight_n}\n"
            f"P&L netto cumulativo: <b>€{total:+.2f}</b>\n"
            f"Capitale PAPER teorico: €{DAILY_BUDGET_EUR:.2f} → <b>€{DAILY_BUDGET_EUR + total:.2f}</b>\n"
            f"Media per trade: <b>€{avg_trade:+.3f}</b>\n"
            f"BUY: <b>€{buy_pnl:+.2f}</b> · SELL: <b>€{sell_pnl:+.2f}</b>\n"
            f"Migliore coppia: <b>{escape(best_pair)}</b> €{best_pair_pnl:+.2f}\n"
            f"Peggiore coppia: <b>{escape(worst_pair)}</b> €{worst_pair_pnl:+.2f}"
        )

    def _send_recap(self, trade_date) -> None:
        super()._send_recap(trade_date)
        self.send(self._cumulative_performance(trade_date))
