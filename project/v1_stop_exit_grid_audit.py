"""Diagnostic V1 grid: widen minimum stop until execution costs are a bounded fraction of 1R.

Read-only audit. It keeps the reconstructed V1 entries unchanged and tests alternative
stop widths/targets with Binance execution costs. It never changes the live Velez bot.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any
import statistics

from project.v1_historical_audit import V1HistoricalAudit, Trade, SYMBOLS


class V1StopExitGridAudit:
    CAPS = (0.10, 0.20, 0.30)
    TARGETS = (1.5, 2.0, 3.0)
    PARTIALS = (
        (0.50, 1.5, 3.0, "50% @1.5R + 50% @3R"),
        (0.50, 2.0, 3.0, "50% @2R + 50% @3R"),
        (0.70, 1.5, 3.0, "70% @1.5R + 30% @3R"),
    )

    def __init__(self, audit: V1HistoricalAudit, logger: Any) -> None:
        self.audit = audit
        self.logger = logger

    def _roundtrip_rate(self) -> float:
        # Conservative approximation at entry notional; exact exit fee is calculated later.
        return self.audit.buy_fee_rate + self.audit.sell_fee_rate + self.audit.spread_rate + 2.0 * self.audit.slippage_rate

    def _widen(self, t: Trade, cap: float) -> Trade:
        min_risk_pct = self._roundtrip_rate() / cap
        risk = max(t.risk, t.entry * min_risk_pct)
        stop = t.entry - risk if t.direction == "LONG" else t.entry + risk
        return replace(t, stop=stop, tp1=t.entry + (1.5*risk if t.direction == "LONG" else -1.5*risk), tp2=t.entry + (3.0*risk if t.direction == "LONG" else -3.0*risk))

    def _net_leg(self, t: Trade, exit_price: float, fraction: float, *, include_entry: bool) -> float:
        notional = self.audit.notional_eur * fraction
        qty = notional / t.entry
        gross = qty * ((exit_price - t.entry) if t.direction == "LONG" else (t.entry - exit_price))
        entry_fee = notional * self.audit.buy_fee_rate if include_entry else 0.0
        exit_notional = qty * exit_price
        exit_fee = exit_notional * self.audit.sell_fee_rate
        spread = qty * (t.entry + exit_price) * (self.audit.spread_rate / 2.0)
        slippage = qty * (t.entry + exit_price) * self.audit.slippage_rate
        return gross - entry_fee - exit_fee - spread - slippage

    def _fixed(self, t: Trade, future: Any, target_r: float) -> tuple[str, float] | None:
        r = self.audit._resolve_fixed(t, future, target_r, stop_first=True)
        if r.outcome not in {"TP", "SL"} or r.exit_price is None:
            return None
        return r.outcome, self._net_leg(t, float(r.exit_price), 1.0, include_entry=True)

    def _partial(self, t: Trade, future: Any, first_fraction: float, first_r: float, runner_r: float) -> tuple[str, float] | None:
        risk = t.risk
        first_price = t.entry + first_r*risk if t.direction == "LONG" else t.entry - first_r*risk
        runner_price = t.entry + runner_r*risk if t.direction == "LONG" else t.entry - runner_r*risk
        first_done = False
        pnl = 0.0
        for _, row in future.iterrows():
            high, low = float(row["high"]), float(row["low"])
            stop_hit = low <= t.stop if t.direction == "LONG" else high >= t.stop
            first_hit = high >= first_price if t.direction == "LONG" else low <= first_price
            runner_hit = high >= runner_price if t.direction == "LONG" else low <= runner_price

            # Conservative intrabar ordering: adverse stop wins whenever ordering is unknowable.
            if stop_hit:
                remaining = 1.0 - first_fraction if first_done else 1.0
                pnl += self._net_leg(t, t.stop, remaining, include_entry=not first_done)
                return "SL_AFTER_PARTIAL" if first_done else "SL", pnl

            if not first_done and first_hit:
                pnl += self._net_leg(t, first_price, first_fraction, include_entry=True)
                first_done = True

            if first_done and runner_hit:
                # Entry fee for the runner still has to be paid; charge it explicitly here.
                runner_fraction = 1.0 - first_fraction
                pnl += self._net_leg(t, runner_price, runner_fraction, include_entry=True)
                return "RUNNER_TP", pnl
        return None

    @staticmethod
    def _stats(rows: list[tuple[str, float]]) -> dict[str, float]:
        if not rows:
            return {"n": 0, "net": 0.0, "pf": 0.0, "wr": 0.0, "expectancy": 0.0}
        pnls = [x[1] for x in rows]
        wins = [x for x in pnls if x > 0]
        losses = [x for x in pnls if x < 0]
        gp, gl = sum(wins), abs(sum(losses))
        return {
            "n": len(rows), "net": sum(pnls), "pf": gp/gl if gl > 0 else (999.0 if gp > 0 else 0.0),
            "wr": 100.0*len(wins)/len(rows), "expectancy": statistics.mean(pnls),
        }

    def run(self) -> dict[str, Any]:
        self.logger.info("V1_STOP_EXIT_GRID_AUDIT_START")
        signals: list[Trade] = []
        frames: dict[str, Any] = {}
        for pair in SYMBOLS:
            ts, _ = self.audit._signals_for_pair(pair)
            signals.extend(ts)
            frames[pair] = self.audit._indicators(self.audit._frame(pair, "15m"))

        def future(t: Trade):
            f = frames[t.pair]
            return f[f["timestamp"] > t.time]

        grid: list[dict[str, Any]] = []
        for cap in self.CAPS:
            widened = [(self._widen(t, cap), future(t)) for t in signals]
            risk_pcts = [100.0*t.risk_pct for t, _ in widened]
            original_pcts = [100.0*t.risk_pct for t in signals]
            widened_count = sum(1 for (wt, _), ot in zip(widened, signals) if wt.risk > ot.risk + 1e-12)
            for target in self.TARGETS:
                rows = [x for t, f in widened if (x := self._fixed(t, f, target)) is not None]
                grid.append({"cap": cap, "mode": f"TP {target:.1f}R", **self._stats(rows),
                             "median_risk_pct": statistics.median(risk_pcts) if risk_pcts else 0.0,
                             "widened": widened_count})
            for frac, first_r, runner_r, label in self.PARTIALS:
                rows = [x for t, f in widened if (x := self._partial(t, f, frac, first_r, runner_r)) is not None]
                grid.append({"cap": cap, "mode": label, **self._stats(rows),
                             "median_risk_pct": statistics.median(risk_pcts) if risk_pcts else 0.0,
                             "widened": widened_count})

        ranked = sorted(grid, key=lambda x: (x["net"], x["pf"], x["n"]), reverse=True)
        summary = {
            "signals": len(signals), "roundtrip_rate": self._roundtrip_rate(),
            "original_median_risk_pct": statistics.median([100.0*t.risk_pct for t in signals]) if signals else 0.0,
            "grid": grid, "best": ranked[:6],
        }
        self.logger.warning("V1_STOP_EXIT_GRID_AUDIT_SUMMARY %s", summary)
        return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        lines = [
            "🧪 <b>AUDIT V1 · STOP E USCITE</b>",
            f"Segnali ricostruiti: <b>{s.get('signals',0)}</b>",
            f"Costo round-trip modellato: <b>{100*s.get('roundtrip_rate',0):.3f}%</b>",
            f"Stop V1 mediano originale: <b>{s.get('original_median_risk_pct',0):.3f}%</b> del prezzo",
            "",
            "🎯 <b>Vincolo costi / 1R</b>",
            "10% = stop minimo ≈ costo/0,10 · 20% = costo/0,20 · 30% = costo/0,30",
            "Gli ingressi restano identici; cambia solo la distanza minima dello stop e quindi la scala R.",
            "",
            "🏆 <b>Migliori configurazioni · P&L netto</b>",
        ]
        for x in s.get("best", []):
            lines.append(
                f"• costi≤{100*x.get('cap',0):.0f}%R · {x.get('mode')} · n=<b>{x.get('n',0)}</b> · "
                f"WR net+ <b>{x.get('wr',0):.1f}%</b> · PF <b>{x.get('pf',0):.2f}</b> · "
                f"P&L <b>€{x.get('net',0):+.2f}</b> · exp <b>€{x.get('expectancy',0):+.3f}</b>"
            )
        lines += ["", "📐 <b>Distanza stop risultante</b>"]
        seen = set()
        for x in s.get("grid", []):
            cap = x.get("cap")
            if cap in seen:
                continue
            seen.add(cap)
            lines.append(f"• costi≤{100*cap:.0f}%R → stop mediano <b>{x.get('median_risk_pct',0):.2f}%</b> · allargati <b>{x.get('widened',0)}</b> trade")
        lines.append("\n⚠️ Audit diagnostico: nessuna modifica al bot live. Doppio tocco SL/TP nella stessa 15m è trattato conservativamente a favore dello stop.")
        return "\n".join(lines)
