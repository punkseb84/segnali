"""Monitor V6 shadow signals using the same exchange-aware TP/SL rules as live signals."""
from __future__ import annotations

from typing import Any

from project.audited_position_monitor import AuditedPositionMonitor


class ShadowValidationMonitor(AuditedPositionMonitor):
    """Monitor live signals normally and shadow signals silently for admission statistics."""

    def monitor_open_signals(self) -> int:
        live_closed = super().monitor_open_signals()
        shadow_closed = self.monitor_shadow_signals()
        return live_closed + shadow_closed

    def monitor_shadow_signals(self) -> int:
        self.expire_shadow_signals()
        rows = self.postgres.fetch_all(
            """SELECT id, runtime_version, strategy, pair, timeframe, regime, exchange,
                      entry, stop_loss, take_profit, net_profit_tp_eur, net_loss_sl_eur,
                      net_rr, score, reference_candle_time, created_at
            FROM signals.shadow_signals
            WHERE status IN ('NEW', 'OPEN')
            ORDER BY created_at ASC
            LIMIT %s""",
            (self.max_signals_per_cycle,),
        )
        closed = 0
        for row in rows:
            signal = {
                "id": int(row[0]),
                "runtime_version": str(row[1]),
                "strategy": str(row[2]),
                "pair": str(row[3]),
                "timeframe": str(row[4]),
                "regime": str(row[5]),
                "exchange": str(row[6]),
                "entry": float(row[7]),
                "stop_loss": float(row[8]),
                "take_profit": float(row[9]),
                "net_profit_tp1_eur": float(row[10]),
                "net_loss_sl_eur": float(row[11]),
                "net_rr": float(row[12]),
                "score": float(row[13]),
                "reference_candle_time": row[14],
                "created_at": row[15],
            }
            closed += 1 if self.check_shadow_signal(signal) else 0
        if rows:
            self.logger.info(
                "SHADOW_MONITOR checked=%s closed=%s", len(rows), closed
            )
        return closed

    def expire_shadow_signals(self) -> None:
        self.postgres.execute(
            """UPDATE signals.shadow_signals
            SET status = 'EXPIRED',
                closed_at = COALESCE(closed_at, NOW()),
                outcome_resolution = COALESCE(outcome_resolution, 'MAX_LIFETIME_48H')
            WHERE status IN ('NEW', 'OPEN')
              AND created_at < NOW() - INTERVAL '48 hours'"""
        )

    def check_shadow_signal(self, signal: dict[str, Any]) -> bool:
        reference_time = signal.get("reference_candle_time")
        start_time = reference_time or signal["created_at"]
        comparison = ">=" if reference_time is not None else ">"
        candles = self.postgres.fetch_all(
            f"""SELECT timestamp, open, high, low, close
            FROM market_data.ohlc
            WHERE exchange = %s
              AND pair = %s
              AND timeframe = %s
              AND timestamp {comparison} %s
            ORDER BY timestamp ASC
            LIMIT 200""",
            (
                signal["exchange"],
                signal["pair"],
                signal["timeframe"],
                start_time,
            ),
        )
        for timestamp, open_price, high, low, close in candles:
            open_f = float(open_price)
            high_f = float(high)
            low_f = float(low)
            close_f = float(close)
            entry_candle = reference_time is not None and timestamp == reference_time
            if entry_candle:
                outcome = self.resolve_close_outcome(
                    "LONG", close_f, signal["take_profit"], signal["stop_loss"]
                )
                resolution = "ENTRY_CANDLE_CLOSE"
                ambiguous = False
            else:
                outcome = self.resolve_candle_outcome(
                    "LONG",
                    high_f,
                    low_f,
                    signal["take_profit"],
                    signal["stop_loss"],
                    self.ambiguous_candle_mode,
                )
                resolution = "FULL_CANDLE_RANGE"
                ambiguous = (
                    high_f >= signal["take_profit"]
                    and low_f <= signal["stop_loss"]
                )
            if outcome is None:
                continue
            outcome_price = (
                signal["stop_loss"]
                if outcome == "STOP_LOSS"
                else signal["take_profit"]
            )
            self.close_shadow_signal(
                signal,
                outcome,
                outcome_price,
                timestamp,
                open_f,
                high_f,
                low_f,
                close_f,
                ambiguous,
                resolution,
            )
            return True
        return False

    def close_shadow_signal(
        self,
        signal: dict[str, Any],
        outcome: str,
        outcome_price: float,
        timestamp: Any,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        ambiguous: bool,
        resolution: str,
    ) -> None:
        self.postgres.execute(
            """UPDATE signals.shadow_signals
            SET status = %s,
                outcome_price = %s,
                outcome_open_price = %s,
                outcome_high_price = %s,
                outcome_low_price = %s,
                outcome_close_price = %s,
                outcome_candle_time = %s,
                outcome_ambiguous = %s,
                outcome_resolution = %s,
                closed_at = NOW()
            WHERE id = %s
              AND status IN ('NEW', 'OPEN')""",
            (
                outcome,
                outcome_price,
                open_price,
                high_price,
                low_price,
                close_price,
                timestamp,
                ambiguous,
                resolution,
                signal["id"],
            ),
        )
        self.logger.info(
            "SHADOW_CLOSED id=%s runtime=%s strategy=%s pair=%s regime=%s "
            "outcome=%s high=%.8f low=%.8f ambiguous=%s",
            signal["id"],
            signal["runtime_version"],
            signal["strategy"],
            signal["pair"],
            signal["regime"],
            outcome,
            high_price,
            low_price,
            ambiguous,
        )
