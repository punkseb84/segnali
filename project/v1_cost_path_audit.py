"""Diagnostic cost decomposition and post-MFE path audit for original V1 signals."""
from __future__ import annotations

from typing import Any
import statistics

from project.v1_historical_audit import V1HistoricalAudit, Trade, SYMBOLS


class V1CostPathAudit:
    def __init__(self, audit: V1HistoricalAudit, logger: Any) -> None:
        self.audit = audit
        self.logger = logger

    def _cost_parts(self, t: Trade) -> dict[str, float]:
        if t.exit_price is None or t.entry <= 0:
            return {"gross": 0.0, "entry_fee": 0.0, "exit_fee": 0.0, "spread": 0.0, "slippage": 0.0, "net": 0.0, "cost_r": 0.0}
        qty = self.audit.notional_eur / t.entry
        gross = qty * ((t.exit_price - t.entry) if t.direction == "LONG" else (t.entry - t.exit_price))
        entry_fee = self.audit.notional_eur * self.audit.buy_fee_rate
        exit_notional = qty * t.exit_price
        exit_fee = exit_notional * self.audit.sell_fee_rate
        spread = qty * (t.entry + t.exit_price) * (self.audit.spread_rate / 2.0)
        slippage = qty * (t.entry + t.exit_price) * self.audit.slippage_rate
        costs = entry_fee + exit_fee + spread + slippage
        risk_eur = qty * t.risk
        return {
            "gross": gross,
            "entry_fee": entry_fee,
            "exit_fee": exit_fee,
            "spread": spread,
            "slippage": slippage,
            "net": gross - costs,
            "cost_r": costs / risk_eur if risk_eur > 0 else 0.0,
        }

    @staticmethod
    def _hit_r(t: Trade, row: Any, level_r: float) -> bool:
        level = t.entry + level_r * t.risk if t.direction == "LONG" else t.entry - level_r * t.risk
        return float(row["high"]) >= level if t.direction == "LONG" else float(row["low"]) <= level

    @staticmethod
    def _stop_hit(t: Trade, row: Any) -> bool:
        return float(row["low"]) <= t.stop if t.direction == "LONG" else float(row["high"]) >= t.stop

    def _path_summary(self, t: Trade, future: Any, trigger_r: float) -> dict[str, Any] | None:
        triggered = False
        trigger_index = None
        peak_r = 0.0
        min_close_after = None
        max_close_after = None
        reached = {0.75: False, 1.0: False, 1.5: False, 2.0: False, 3.0: False}
        eventually_stopped = False
        bars_after = 0
        for idx, (_, row) in enumerate(future.iterrows()):
            if not triggered:
                if self._stop_hit(t, row) and self._hit_r(t, row, trigger_r):
                    # Intrabar ordering unknown; exclude this path from behavioral statistics.
                    return None
                if self._stop_hit(t, row):
                    return None
                if self._hit_r(t, row, trigger_r):
                    triggered = True
                    trigger_index = idx
                else:
                    continue
            bars_after += 1
            high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
            fav_r = ((high - t.entry) / t.risk) if t.direction == "LONG" else ((t.entry - low) / t.risk)
            close_r = ((close - t.entry) / t.risk) if t.direction == "LONG" else ((t.entry - close) / t.risk)
            peak_r = max(peak_r, fav_r)
            min_close_after = close_r if min_close_after is None else min(min_close_after, close_r)
            max_close_after = close_r if max_close_after is None else max(max_close_after, close_r)
            for level in reached:
                if fav_r >= level:
                    reached[level] = True
            if self._stop_hit(t, row):
                eventually_stopped = True
                break
        if not triggered:
            return None
        return {
            "trigger_r": trigger_r,
            "peak_r": peak_r,
            "eventually_stopped": eventually_stopped,
            "bars_after": bars_after,
            "reached": reached,
            "min_close_after": min_close_after or 0.0,
            "max_close_after": max_close_after or 0.0,
            "giveback_r": max(0.0, peak_r - (min_close_after or 0.0)),
        }

    def run(self) -> dict[str, Any]:
        self.logger.info("V1_COST_PATH_AUDIT_START")
        signals: list[Trade] = []
        frames: dict[str, Any] = {}
        for pair in SYMBOLS:
            ts, _ = self.audit._signals_for_pair(pair)
            signals.extend(ts)
            frames[pair] = self.audit._indicators(self.audit._frame(pair, "15m"))

        def future(t: Trade):
            f = frames[t.pair]
            return f[f["timestamp"] > t.time]

        original = [self.audit._resolve_fixed(t, future(t), 1.5) for t in signals]
        resolved = [t for t in original if t.outcome in {"TP", "SL"}]
        parts = [self._cost_parts(t) for t in resolved]
        total_entry_fee = sum(x["entry_fee"] for x in parts)
        total_exit_fee = sum(x["exit_fee"] for x in parts)
        total_spread = sum(x["spread"] for x in parts)
        total_slippage = sum(x["slippage"] for x in parts)
        total_cost = total_entry_fee + total_exit_fee + total_spread + total_slippage
        gross_pnl = sum(x["gross"] for x in parts)
        net_pnl = sum(x["net"] for x in parts)
        cost_rs = [x["cost_r"] for x in parts if x["cost_r"] >= 0]

        # Sanity scenarios on the exact same resolved trades.
        fees_only_net = sum(x["gross"] - x["entry_fee"] - x["exit_fee"] for x in parts)
        no_cost_net = gross_pnl

        path = {}
        for trigger in (0.50, 0.75, 1.00):
            rows = []
            for t in signals:
                p = self._path_summary(t, future(t), trigger)
                if p is not None:
                    rows.append(p)
            n = len(rows)
            path[f"{trigger:.2f}R"] = {
                "count": n,
                "stopped_after": sum(1 for x in rows if x["eventually_stopped"]),
                "to_075": sum(1 for x in rows if x["reached"][0.75]),
                "to_100": sum(1 for x in rows if x["reached"][1.0]),
                "to_150": sum(1 for x in rows if x["reached"][1.5]),
                "to_200": sum(1 for x in rows if x["reached"][2.0]),
                "to_300": sum(1 for x in rows if x["reached"][3.0]),
                "avg_peak_r": statistics.mean([x["peak_r"] for x in rows]) if rows else 0.0,
                "avg_giveback_r": statistics.mean([x["giveback_r"] for x in rows]) if rows else 0.0,
                "median_bars_after": statistics.median([x["bars_after"] for x in rows]) if rows else 0.0,
            }

        summary = {
            "trades": len(resolved),
            "notional": self.audit.notional_eur,
            "buy_fee_rate": self.audit.buy_fee_rate,
            "sell_fee_rate": self.audit.sell_fee_rate,
            "spread_rate": self.audit.spread_rate,
            "slippage_rate": self.audit.slippage_rate,
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "no_cost_net": no_cost_net,
            "fees_only_net": fees_only_net,
            "entry_fees": total_entry_fee,
            "exit_fees": total_exit_fee,
            "spread_cost": total_spread,
            "slippage_cost": total_slippage,
            "total_cost": total_cost,
            "avg_cost_trade": total_cost / len(resolved) if resolved else 0.0,
            "avg_cost_r": statistics.mean(cost_rs) if cost_rs else 0.0,
            "median_cost_r": statistics.median(cost_rs) if cost_rs else 0.0,
            "path": path,
        }
        self.logger.warning("V1_COST_PATH_AUDIT_SUMMARY %s", summary)
        return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        lines = [
            "🔎 <b>AUDIT V1 · COSTI E PERCORSO PROFITTO</b>",
            f"Trade analizzati: <b>{s.get('trades',0)}</b> · capitale/trade: <b>€{s.get('notional',0):.2f}</b>",
            f"Fee entrata: <b>{100*s.get('buy_fee_rate',0):.3f}%</b> · fee uscita: <b>{100*s.get('sell_fee_rate',0):.3f}%</b>",
            f"Spread modellato: <b>{100*s.get('spread_rate',0):.3f}%</b> · slippage: <b>{100*s.get('slippage_rate',0):.3f}%</b>",
            "",
            "💸 <b>Scomposizione economica</b>",
            f"P&L lordo senza costi: <b>€{s.get('gross_pnl',0):+.2f}</b>",
            f"P&L con sole fee: <b>€{s.get('fees_only_net',0):+.2f}</b>",
            f"P&L con tutti i costi: <b>€{s.get('net_pnl',0):+.2f}</b>",
            f"Fee entrata totali: <b>€{s.get('entry_fees',0):.2f}</b>",
            f"Fee uscita totali: <b>€{s.get('exit_fees',0):.2f}</b>",
            f"Spread totale: <b>€{s.get('spread_cost',0):.2f}</b>",
            f"Slippage totale: <b>€{s.get('slippage_cost',0):.2f}</b>",
            f"Costo medio/trade: <b>€{s.get('avg_cost_trade',0):.3f}</b> · <b>{s.get('avg_cost_r',0):.2f}R</b>",
            f"Costo mediano: <b>{s.get('median_cost_r',0):.2f}R</b>",
            "",
            "📈 <b>Dopo aver raggiunto profitto</b>",
        ]
        for label, x in s.get("path", {}).items():
            n = x.get("count", 0)
            pct = lambda key: (100*x.get(key,0)/n) if n else 0.0
            lines.append(
                f"• Da +{label}: n=<b>{n}</b> · poi +1R <b>{pct('to_100'):.1f}%</b> · +1,5R <b>{pct('to_150'):.1f}%</b> · +2R <b>{pct('to_200'):.1f}%</b> · poi SL <b>{pct('stopped_after'):.1f}%</b>"
            )
            lines.append(
                f"  picco medio <b>+{x.get('avg_peak_r',0):.2f}R</b> · giveback medio <b>{x.get('avg_giveback_r',0):.2f}R</b> · barre mediane <b>{x.get('median_bars_after',0):.0f}</b>"
            )
        lines.append("\n⚠️ Diagnostico: nessuna modifica al bot live. Le candele con ordine intrabar non determinabile sono escluse dall'analisi del percorso.")
        return "\n".join(lines)
