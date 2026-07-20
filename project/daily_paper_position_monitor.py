"""Position monitor that preserves PAPER_DAILY/LIVE_ADMITTED labels in outcome events."""
from __future__ import annotations

from typing import Any

from project.fair_shadow_signal_monitor import FairShadowValidationMonitor


class DailyPaperPositionMonitor(FairShadowValidationMonitor):
    def fetch_open_signals(self) -> list[dict[str, Any]]:
        rows = self.postgres.fetch_all(
            """SELECT id, strategy, pair, timeframe, regime, entry, stop_loss, take_profit,
                      score, probability, signal_class,
                      net_profit_tp1_eur, net_loss_sl_eur, net_rr,
                      reference_candle_time, created_at,
                      COALESCE(exchange, 'kraken'),
                      COALESCE(score_breakdown->>'execution_mode', '')
            FROM signals.generated_signals
            WHERE status IN ('NEW', 'OPEN')
              AND signal_class IN ('A', 'B')
              AND telegram_sent_at IS NOT NULL
            ORDER BY created_at ASC
            LIMIT %s""",
            (self.max_signals_per_cycle,),
        )
        signals: list[dict[str, Any]] = []
        for row in rows:
            signals.append(
                {
                    "id": row[0],
                    "strategy": row[1],
                    "pair": row[2],
                    "timeframe": row[3],
                    "regime": row[4],
                    "entry": float(row[5]),
                    "stop_loss": float(row[6]),
                    "take_profit": float(row[7]),
                    "score": float(row[8]),
                    "probability": float(row[9]) if row[9] is not None else None,
                    "signal_class": row[10],
                    "net_profit_tp1_eur": (
                        float(row[11]) if row[11] is not None else None
                    ),
                    "net_loss_sl_eur": (
                        float(row[12]) if row[12] is not None else None
                    ),
                    "net_rr": float(row[13]) if row[13] is not None else None,
                    "reference_candle_time": row[14],
                    "created_at": row[15],
                    "exchange": str(row[16] or "kraken"),
                    "execution_mode": str(row[17] or ""),
                }
            )
        return signals
