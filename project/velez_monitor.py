"""Position lifecycle for Velez intraday Mode 2 with profit protection.

There is no fixed take profit. The original structural stop is active from entry.
When price reaches +1.5R, the full position remains open and the stop is promoted
to +1R. Afterwards the position exits on that protected stop, on a CLOSED 15m
candle crossing EMA20 against the trade, or after 12 hours (48 completed 15m bars).

Startup reconciliation also repairs legacy Velez signals that were mechanically
marked EXPIRED during a runtime migration before their real stop/target outcome
was persisted.
"""
from __future__ import annotations

import json
from typing import Any

import pandas as pd

from project.relative_strength_monitor import RelativeStrengthMonitor
from project.shared.events import Event, EventType


class Velez15mMonitor(RelativeStrengthMonitor):
    MAX_HOLD_CANDLES = 48
    PROTECTION_TRIGGER_R = 1.5
    PROTECTED_STOP_R = 1.0

    @staticmethod
    def _levels(signal: dict[str, Any], direction: str) -> tuple[float, float, float, float]:
        entry = float(signal["entry"])
        hard_stop = float(signal["stop_loss"])
        risk = abs(entry - hard_stop)
        trigger = entry + 1.5 * risk if direction == "LONG" else entry - 1.5 * risk
        protected_stop = entry + 1.0 * risk if direction == "LONG" else entry - 1.0 * risk
        return entry, hard_stop, trigger, protected_stop

    @staticmethod
    def _decode_state(raw: Any) -> dict[str, Any]:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        return dict(raw or {}) if isinstance(raw, dict) else {}

    def _all_active_signals(self) -> list[dict[str, Any]]:
        """Load every delivered NEW/OPEN signal, regardless of legacy runtime version."""
        rows = self.postgres.fetch_all(
            """SELECT id, strategy, pair, timeframe, entry, stop_loss, take_profit,
                      reference_candle_time, created_at, telegram_sent_at,
                      COALESCE(exchange, 'Kraken'), quantity,
                      COALESCE(estimated_buy_fee_eur, 0),
                      COALESCE(score_breakdown, '{}'::jsonb)
               FROM signals.generated_signals
               WHERE status IN ('NEW','OPEN')
                 AND telegram_sent_at IS NOT NULL
               ORDER BY created_at ASC"""
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "id": int(row[0]), "strategy": str(row[1]), "pair": str(row[2]),
                    "timeframe": str(row[3]), "entry": float(row[4]),
                    "stop_loss": float(row[5]), "take_profit": float(row[6] or 0.0),
                    "reference_candle_time": row[7], "created_at": row[8],
                    "telegram_sent_at": row[9], "exchange": str(row[10]),
                    "quantity": float(row[11] or 0.0),
                    "estimated_buy_fee_eur": float(row[12] or 0.0),
                    "state": self._decode_state(row[13]),
                }
            )
        return result

    def _migration_expired_signals(self) -> list[dict[str, Any]]:
        """Load only Velez signals auto-expired by a strategy/runtime migration."""
        rows = self.postgres.fetch_all(
            """SELECT id, strategy, pair, timeframe, entry, stop_loss, take_profit,
                      reference_candle_time, created_at, telegram_sent_at,
                      COALESCE(exchange, 'Kraken'), quantity,
                      COALESCE(estimated_buy_fee_eur, 0),
                      COALESCE(score_breakdown, '{}'::jsonb), closed_at,
                      COALESCE(outcome_resolution,'')
               FROM signals.generated_signals
               WHERE status='EXPIRED'
                 AND telegram_sent_at IS NOT NULL
                 AND COALESCE(outcome_resolution,'') LIKE 'REPLACED_BY_VELEZ%'
               ORDER BY created_at ASC"""
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "id": int(row[0]), "strategy": str(row[1]), "pair": str(row[2]),
                    "timeframe": str(row[3]), "entry": float(row[4]),
                    "stop_loss": float(row[5]), "take_profit": float(row[6] or 0.0),
                    "reference_candle_time": row[7], "created_at": row[8],
                    "telegram_sent_at": row[9], "exchange": str(row[10]),
                    "quantity": float(row[11] or 0.0),
                    "estimated_buy_fee_eur": float(row[12] or 0.0),
                    "state": self._decode_state(row[13]),
                    "archived_at": row[14], "previous_resolution": str(row[15]),
                }
            )
        return result

    def _historical_stop_row(self, signal: dict[str, Any]) -> dict[str, Any] | None:
        """Return first completed 15m candle after entry that touched the stored hard stop."""
        exchange = str(signal.get("exchange") or "Kraken")
        direction = str(signal["state"].get("direction") or "LONG").upper()
        stop = float(signal["stop_loss"])
        reference = self._utc(
            signal.get("reference_candle_time")
            or signal.get("telegram_sent_at")
            or signal.get("created_at")
        ).floor("15min")
        comparator = "low <= %s" if direction == "LONG" else "high >= %s"
        rows = self.postgres.fetch_all(
            f"""SELECT timestamp, open, high, low, close
                  FROM market_data.ohlc
                 WHERE LOWER(TRIM(exchange)) = LOWER(TRIM(%s))
                   AND pair = %s AND timeframe = '15m'
                   AND timestamp > %s AND {comparator}
                 ORDER BY timestamp ASC LIMIT 1""",
            (exchange, signal["pair"], reference.to_pydatetime(), stop),
        )
        if not rows:
            return None
        ts, open_, high, low, close = rows[0]
        timestamp = self._utc(ts)
        if timestamp + pd.Timedelta(minutes=15) > pd.Timestamp.now(tz="UTC"):
            return None
        return {"timestamp": timestamp, "open": float(open_), "high": float(high),
                "low": float(low), "close": float(close)}

    def _legacy_first_terminal_event(self, signal: dict[str, Any]) -> tuple[str, float, dict[str, Any]] | None:
        """Find first stop/active-target event before an automatic migration archive.

        For Mode2+ runtimes the stored 1.5R price is accounting metadata and is not
        treated as a target. For older runtimes the historical target remains active.
        Same-candle stop+target is deliberately marked ambiguous.
        """
        archived_at = signal.get("archived_at")
        if archived_at is None:
            return None
        archive = self._utc(archived_at)
        reference = self._utc(
            signal.get("reference_candle_time")
            or signal.get("telegram_sent_at")
            or signal.get("created_at")
        ).floor("15min")
        exchange = str(signal.get("exchange") or "Kraken")
        rows = self.postgres.fetch_all(
            """SELECT timestamp, open, high, low, close
                 FROM market_data.ohlc
                WHERE LOWER(TRIM(exchange)) = LOWER(TRIM(%s))
                  AND pair=%s AND timeframe='15m'
                  AND timestamp > %s
                  AND timestamp + INTERVAL '15 minutes' <= %s
                ORDER BY timestamp ASC""",
            (exchange, signal["pair"], reference.to_pydatetime(), archive.to_pydatetime()),
        )
        if not rows:
            return None

        direction = str(signal["state"].get("direction") or "LONG").upper()
        stop = float(signal["stop_loss"])
        target = float(signal.get("take_profit") or 0.0)
        state = signal["state"]
        runtime = str(state.get("runtime_version") or "")
        target_active = bool(target > 0)
        if (
            state.get("fixed_target_active") is False
            or bool(state.get("stored_target_accounting_only"))
            or "MODE2" in runtime
        ):
            target_active = False

        for ts, open_, high_, low_, close_ in rows:
            row = {
                "timestamp": self._utc(ts), "open": float(open_), "high": float(high_),
                "low": float(low_), "close": float(close_),
            }
            high, low = row["high"], row["low"]
            stop_hit = low <= stop if direction == "LONG" else high >= stop
            target_hit = target_active and (
                high >= target if direction == "LONG" else low <= target
            )
            if stop_hit and target_hit:
                return "VELEZ_LEGACY_AMBIGUOUS_BOTH_HIT", float(row["close"]), row
            if stop_hit:
                return "VELEZ_MIGRATION_HARD_STOP_RECONCILIATION", stop, row
            if target_hit:
                return "VELEZ_MIGRATION_TARGET_RECONCILIATION", target, row
        return None

    def _close_expired_reconciled(
        self, signal: dict[str, Any], price: float, row: dict[str, Any], resolution: str
    ) -> bool:
        """Replace an automatic EXPIRED migration marker with its real market outcome."""
        pnl = self._pnl(signal, price)
        result = float(pnl["net_pnl_eur"])
        if resolution == "VELEZ_LEGACY_AMBIGUOUS_BOTH_HIT":
            status = "STOP_LOSS" if result < 0 else "TARGET_HIT"
        elif "TARGET" in resolution:
            status = "TARGET_HIT"
        else:
            status = "STOP_LOSS"
        net_profit, net_loss = max(result, 0.0), max(-result, 0.0)
        patch = {
            "gross_pnl_eur": pnl["gross_pnl_eur"],
            "entry_fee_eur": pnl["entry_fee_eur"],
            "exit_fee_eur": pnl["exit_fee_eur"],
            "spread_cost_eur": pnl["spread_cost_eur"],
            "slippage_cost_eur": pnl["slippage_cost_eur"],
            "total_costs_eur": pnl["total_costs_eur"],
            "migration_reconciled": True,
            "migration_previous_resolution": signal.get("previous_resolution"),
        }
        updated = self.postgres.execute(
            """UPDATE signals.generated_signals
                  SET status=%s, outcome_price=%s, outcome_open_price=%s,
                      outcome_high_price=%s, outcome_low_price=%s, outcome_close_price=%s,
                      outcome_candle_time=%s, closed_at=%s, outcome_ambiguous=%s,
                      outcome_resolution=%s, net_profit_tp1_eur=%s, net_loss_sl_eur=%s,
                      score_breakdown=COALESCE(score_breakdown,'{}'::jsonb)||%s::jsonb
                WHERE id=%s AND status='EXPIRED'
                  AND COALESCE(outcome_resolution,'') LIKE 'REPLACED_BY_VELEZ%'""",
            (
                status, price, float(row["open"]), float(row["high"]), float(row["low"]),
                float(row["close"]), row["timestamp"], row["timestamp"],
                resolution == "VELEZ_LEGACY_AMBIGUOUS_BOTH_HIT", resolution,
                net_profit, net_loss, json.dumps(patch), signal["id"],
            ),
        )
        self.logger.warning(
            "VELEZ_MIGRATION_RECONCILED id=%s pair=%s resolution=%s status=%s price=%.8f net=%.4f",
            signal["id"], signal["pair"], resolution, status, price, result,
        )
        payload = {
            **signal, **patch, "signal_id": signal["id"],
            "direction": signal["state"].get("direction", "LONG"),
            "outcome": status, "outcome_price": price,
            "close_price": float(row["close"]), "closed_at": str(row["timestamp"]),
            "outcome_resolution": resolution, "realized_net_eur": result,
            "net_profit_tp1_eur": net_profit, "net_loss_sl_eur": net_loss,
        }
        self.event_bus.publish(
            Event(EventType.TARGET_HIT if status == "TARGET_HIT" else EventType.STOP_LOSS, payload)
        )
        return bool(updated is not False)

    def reconcile_migration_expired_outcomes(self) -> int:
        """Repair migration-expired Velez signals when a real stop/target occurred first."""
        signals = self._migration_expired_signals()
        repaired = 0
        self.logger.info(
            "VELEZ_MIGRATION_OUTCOME_AUDIT expired=%s ids=%s",
            len(signals), [signal["id"] for signal in signals],
        )
        for signal in signals:
            try:
                event = self._legacy_first_terminal_event(signal)
                if event is None:
                    self.logger.info(
                        "VELEZ_MIGRATION_OUTCOME_NONE id=%s pair=%s archived_at=%s",
                        signal["id"], signal["pair"], signal.get("archived_at"),
                    )
                    continue
                resolution, price, row = event
                if self._close_expired_reconciled(signal, price, row, resolution):
                    repaired += 1
            except Exception:
                self.logger.exception(
                    "VELEZ_MIGRATION_OUTCOME_AUDIT_FAILED id=%s pair=%s",
                    signal.get("id"), signal.get("pair"),
                )
        self.logger.info(
            "VELEZ_MIGRATION_OUTCOME_AUDIT_DONE expired=%s repaired=%s",
            len(signals), repaired,
        )
        return repaired

    def reconcile_all_active_hard_stops(self) -> int:
        """One-shot startup audit: close every active signal whose stored stop has been reached."""
        active = self._all_active_signals()
        closed = 0
        self.logger.info("VELEZ_STARTUP_STOP_AUDIT active=%s ids=%s", len(active), [s["id"] for s in active])
        for signal in active:
            try:
                direction = str(signal["state"].get("direction") or "LONG").upper()
                stop = float(signal["stop_loss"])
                if str(signal.get("exchange") or "Kraken").lower() == "kraken":
                    live = self._live_price(signal["pair"])
                    live_hit = live is not None and (live <= stop if direction == "LONG" else live >= stop)
                    if live_hit:
                        self.logger.warning(
                            "VELEZ_STARTUP_STOP_HIT_LIVE id=%s pair=%s direction=%s live=%.8f stop=%.8f",
                            signal["id"], signal["pair"], direction, float(live), stop,
                        )
                        if self._close(signal, stop, self._live_row(float(live)), "VELEZ_STARTUP_HARD_STOP_RECONCILIATION"):
                            closed += 1
                        continue
                row = self._historical_stop_row(signal)
                if row is not None:
                    self.logger.warning(
                        "VELEZ_STARTUP_STOP_HIT_HISTORY id=%s pair=%s direction=%s candle=%s high=%.8f low=%.8f stop=%.8f",
                        signal["id"], signal["pair"], direction, row["timestamp"], row["high"], row["low"], stop,
                    )
                    if self._close(signal, stop, row, "VELEZ_STARTUP_HARD_STOP_RECONCILIATION"):
                        closed += 1
                else:
                    self.logger.info("VELEZ_STARTUP_STOP_CLEAR id=%s pair=%s direction=%s stop=%.8f", signal["id"], signal["pair"], direction, stop)
            except Exception:
                self.logger.exception("VELEZ_STARTUP_STOP_AUDIT_FAILED id=%s pair=%s", signal.get("id"), signal.get("pair"))
        self.logger.info("VELEZ_STARTUP_STOP_AUDIT_DONE active=%s closed=%s", len(active), closed)
        return closed

    def _persist_protection(self, signal: dict[str, Any], trigger: float, protected_stop: float) -> None:
        state = signal["state"]
        if bool(state.get("profit_protection_armed")):
            return
        patch = {
            "profit_protection_armed": True,
            "profit_protection_trigger_price": trigger,
            "profit_protected_stop_price": protected_stop,
            "profit_protection_trigger_r": self.PROTECTION_TRIGGER_R,
            "profit_protection_stop_r": self.PROTECTED_STOP_R,
        }
        self.postgres.execute(
            """UPDATE signals.generated_signals
               SET score_breakdown=COALESCE(score_breakdown,'{}'::jsonb)||%s::jsonb
               WHERE id=%s AND status IN ('NEW','OPEN')""",
            (json.dumps(patch), signal["id"]),
        )
        state.update(patch)
        direction = str(state.get("direction", "LONG"))
        message = (
            "🛡️ <b>PROTEZIONE PROFITTO ATTIVATA · PAPER</b>\n"
            f"🆔 <code>#{signal['id']}</code>\n"
            f"<b>{signal['pair']}</b> · <b>{direction}</b> · 15m\n\n"
            f"Raggiunto: <b>+1,5R</b> ({trigger:.8f})\n"
            f"Nuovo stop protetto: <b>+1R</b> ({protected_stop:.8f})\n\n"
            "La posizione resta aperta. Uscita su stop protetto, chiusura 15m contro EMA20 o limite 12 ore."
        )
        self.event_bus.publish(Event(EventType.REPORT_READY, {
            "message": message, "trusted_html": True, "signal_id": signal["id"],
            "pair": signal["pair"], "timeframe": "15m",
            "management_update": "VELEZ_PROFIT_PROTECTION_ARMED",
        }))
        self.logger.info("VELEZ_PROTECTION_ARMED id=%s pair=%s trigger=%.8f protected_stop=%.8f", signal["id"], signal["pair"], trigger, protected_stop)

    def _check_live_levels(self, signal: dict[str, Any]) -> bool:
        if str(signal.get("exchange") or "Kraken").lower() != "kraken":
            return False
        price = self._live_price(signal["pair"])
        if price is None:
            return False
        direction = str(signal["state"].get("direction", "LONG"))
        _, hard_stop, trigger, protected_stop = self._levels(signal, direction)
        armed = bool(signal["state"].get("profit_protection_armed"))
        active_stop = protected_stop if armed else hard_stop
        self.logger.info("VELEZ_MONITOR_LIVE id=%s pair=%s price=%.8f active_stop=%.8f trigger=%.8f armed=%s direction=%s", signal["id"], signal["pair"], price, active_stop, trigger, armed, direction)
        stop_hit = price <= active_stop if direction == "LONG" else price >= active_stop
        if stop_hit:
            reason = "VELEZ_PROTECTED_STOP" if armed else "VELEZ_HARD_STOP"
            return self._close(signal, active_stop, self._live_row(price), reason)
        trigger_hit = price >= trigger if direction == "LONG" else price <= trigger
        if trigger_hit and not armed:
            self._persist_protection(signal, trigger, protected_stop)
        return False

    def _check(self, signal: dict[str, Any]) -> bool:
        if self._check_live_levels(signal):
            return True
        exchange = str(signal.get("exchange") or "Kraken")
        frame = self._frame(signal["pair"], exchange)
        if frame.empty:
            self.logger.warning("VELEZ_MONITOR_NO_FRAME id=%s pair=%s exchange=%s", signal["id"], signal["pair"], exchange)
            return False
        frame = frame.copy()
        frame["ema20"] = frame["close"].ewm(span=20, adjust=False).mean()
        direction = str(signal["state"].get("direction", "LONG"))
        reference = self._utc(signal.get("reference_candle_time") or signal.get("telegram_sent_at") or signal.get("created_at"))
        eligible = frame[frame["timestamp"] > reference.floor("15min")].copy()
        if eligible.empty:
            return False
        _, hard_stop, trigger, protected_stop = self._levels(signal, direction)
        armed = bool(signal["state"].get("profit_protection_armed"))
        max_hold = int(signal["state"].get("max_hold_candles") or self.MAX_HOLD_CANDLES)
        max_hold = max(1, min(max_hold, self.MAX_HOLD_CANDLES))
        self.logger.info("VELEZ_MONITOR_START id=%s pair=%s direction=%s hard_stop=%.8f trigger=%.8f protected_stop=%.8f armed=%s candles=%s max_hold=%s", signal["id"], signal["pair"], direction, hard_stop, trigger, protected_stop, armed, len(eligible), max_hold)
        for index, (_, row) in enumerate(eligible.iterrows(), start=1):
            high, low, close, ema20 = float(row["high"]), float(row["low"]), float(row["close"]), float(row["ema20"])
            active_stop = protected_stop if armed else hard_stop
            stop_hit = low <= active_stop if direction == "LONG" else high >= active_stop
            ema_exit = close < ema20 if direction == "LONG" else close > ema20
            self.logger.info("VELEZ_MONITOR_CANDLE id=%s n=%s candle=%s high=%.8f low=%.8f close=%.8f ema20=%.8f active_stop=%.8f armed=%s stop_hit=%s ema_exit=%s", signal["id"], index, row["timestamp"], high, low, close, ema20, active_stop, armed, stop_hit, ema_exit)
            if stop_hit:
                return self._close(signal, active_stop, row, "VELEZ_PROTECTED_STOP" if armed else "VELEZ_HARD_STOP")
            if ema_exit:
                return self._close(signal, close, row, "VELEZ_EMA20_EXIT")
            if not armed:
                trigger_hit = high >= trigger if direction == "LONG" else low <= trigger
                if trigger_hit:
                    armed = True
                    self._persist_protection(signal, trigger, protected_stop)
            if index >= max_hold:
                return self._close(signal, close, row, "VELEZ_12H_TIME_EXIT")
        return False
