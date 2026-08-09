"""Faster V3 wrapper for the frozen 36m Path Edge Discovery.

The research protocol and all strategy parameters are unchanged.  The only
material change is computational: when the DEV grid is evaluated, signal
indices are pre-filtered vectorially to the requested chronological phase
before the Python trade loop runs.  The prior V2 iterated signal indices from
all 36 months for every DEV configuration and discarded them one by one.
"""
from __future__ import annotations

from typing import Any
import json
import math

import pandas as pd

from project.path_edge_discovery_binance_36m_audit import (
    PathEdgeDiscoveryBinance36mAudit,
    DEV_START,
    DEV_END,
    VAL_END,
    HOLDOUT_END,
    EXTERNAL_A_END,
    EXTERNAL_B_END,
)
from project.event_move_discovery_binance_24m_audit import TF_MINUTES

AUDIT_VERSION = "PATH_EDGE_DISCOVERY_BINANCE_36M_20260809_V3_FAST_PHASE_SCAN"

PHASE_BOUNDS = {
    "DEV": (DEV_START, DEV_END),
    "VAL": (DEV_END, VAL_END),
    "HOLDOUT": (VAL_END, HOLDOUT_END),
    "EXTERNAL_A": (HOLDOUT_END, EXTERNAL_A_END),
    "EXTERNAL_B": (EXTERNAL_A_END, EXTERNAL_B_END),
}


class PathEdgeDiscoveryBinance36mV3Audit(PathEdgeDiscoveryBinance36mAudit):
    def already_done(self) -> bool:
        self._ensure_marker()
        return bool(self.postgres.fetch_all(
            "SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1",
            (AUDIT_VERSION,),
        ))

    def _save_marker(self, summary: dict[str, Any]) -> None:
        self.postgres.execute(
            "INSERT INTO statistics.velez_candidate_audit_runs(version,summary) VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING",
            (AUDIT_VERSION, json.dumps(summary, default=str)),
        )

    def _simulate_pair_candidate(
        self,
        x: pd.DataFrame,
        mask: pd.Series,
        direction: str,
        pair: str,
        tf: str,
        event_name: str,
        tp_pct: float,
        sl_pct: float,
        max_hold_h: int,
        phase_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        trades: list[dict[str, Any]] = []
        max_bars = max(1, int(math.ceil(max_hold_h * 60 / TF_MINUTES[tf])))

        active = mask.fillna(False).astype(bool)
        # Signal is known on bar i; execution is at bar i+1.  Filter using the
        # NEXT bar timestamp so the chronological split remains exact.
        if phase_filter is not None:
            bounds = PHASE_BOUNDS.get(phase_filter)
            if bounds is None:
                return trades
            start, end = bounds
            next_ts = x["timestamp"].shift(-1)
            active = active & (next_ts >= start) & (next_ts < end)

        signal_indices = x.index[active].to_numpy(dtype=int, copy=False)
        if len(signal_indices) == 0:
            return trades

        opens = x["open"].to_numpy(dtype=float, copy=False)
        highs = x["high"].to_numpy(dtype=float, copy=False)
        lows = x["low"].to_numpy(dtype=float, copy=False)
        closes = x["close"].to_numpy(dtype=float, copy=False)
        timestamps = x["timestamp"].to_numpy(copy=False)

        blocked_until = -1
        nrows = len(x)
        for signal_i_raw in signal_indices:
            signal_i = int(signal_i_raw)
            entry_i = signal_i + 1
            if entry_i <= blocked_until or entry_i >= nrows:
                continue

            entry_time = pd.Timestamp(timestamps[entry_i])
            phase = phase_filter if phase_filter is not None else self._phase(entry_time)
            if phase is None:
                continue

            entry = float(opens[entry_i])
            if not math.isfinite(entry) or entry <= 0:
                continue

            if direction == "LONG":
                target = entry * (1.0 + tp_pct)
                stop = entry * (1.0 - sl_pct)
            else:
                target = entry * (1.0 - tp_pct)
                stop = entry * (1.0 + sl_pct)

            last_i = min(nrows - 1, entry_i + max_bars - 1)
            if pd.Timestamp(timestamps[last_i]) >= EXTERNAL_B_END:
                break

            exit_px: float | None = None
            reason = "TIMEOUT"
            exit_i = last_i
            for k in range(entry_i, last_i + 1):
                op = float(opens[k])
                hi = float(highs[k])
                lo = float(lows[k])
                if direction == "LONG":
                    if op <= stop:
                        exit_px, reason, exit_i = op, "STOP_GAP", k
                        break
                    if op >= target:
                        exit_px, reason, exit_i = op, "TARGET_GAP", k
                        break
                    if lo <= stop:
                        exit_px, reason, exit_i = stop, "STOP", k
                        break
                    if hi >= target:
                        exit_px, reason, exit_i = target, "TARGET", k
                        break
                else:
                    if op >= stop:
                        exit_px, reason, exit_i = op, "STOP_GAP", k
                        break
                    if op <= target:
                        exit_px, reason, exit_i = op, "TARGET_GAP", k
                        break
                    if hi >= stop:
                        exit_px, reason, exit_i = stop, "STOP", k
                        break
                    if lo <= target:
                        exit_px, reason, exit_i = target, "TARGET", k
                        break

            if exit_px is None:
                exit_px = float(closes[last_i])

            gross, fee_only, net = self._net_return(entry, float(exit_px), direction)
            trades.append({
                "pair": pair,
                "tf": tf,
                "event": event_name,
                "direction": direction,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "max_hold_h": max_hold_h,
                "entry_time": entry_time,
                "exit_time": pd.Timestamp(timestamps[exit_i]),
                "phase": phase,
                "gross_r": gross,
                "fee_r": fee_only,
                "net_r": net,
                "net_eur": self.notional * net,
                "reason": reason,
                "bars": exit_i - entry_i + 1,
            })
            blocked_until = exit_i

        return trades
