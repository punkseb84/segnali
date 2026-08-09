"""Cost-sensitivity diagnostic for the frozen 36m Path Edge families.

Protocol:
- Reproduce the exact Path Edge V3 family selection on DEV using the configured
  real economic cost model.  No event, timeframe, TP, SL or holding parameter is
  selected using later periods.
- Freeze exactly one configuration for each of 18 events x 5 timeframes.
- Re-run those frozen configurations through DEV, VALIDATION, HOLDOUT,
  EXTERNAL_A and EXTERNAL_B.
- Revalue the SAME gross trade paths at flat equivalent round-trip frictions of
  37, 25, 15, 10, 5 and 0 bps.  This is a diagnostic sensitivity curve, not a
  claim that those alternative fee schedules are currently obtainable.
- Report both a permissive all-phases-positive count and a stronger replicated
  count requiring the final external bootstrap lower bound above zero.

The key maximin statistic is the minimum gross expectancy across the five
chronological phases.  Expressed in bps, it is the maximum flat round-trip cost
that the weakest phase could tolerate before its mean expectancy reaches zero.
"""
from __future__ import annotations

from typing import Any
import json
import random
import statistics

from project.path_edge_discovery_binance_36m_v3_audit import PathEdgeDiscoveryBinance36mV3Audit
from project.path_edge_discovery_binance_36m_audit import (
    BARRIERS,
    MAX_HOLDS_H,
    MIN_DEV,
    MIN_VAL,
    MIN_HOLDOUT,
    MIN_EXTERNAL,
)
from project.event_move_discovery_binance_24m_audit import TIMEFRAMES
from project.cryptoresearch_v1_cand000006_binance_12m_audit import SYMBOLS, PAIR_NAMES

AUDIT_VERSION = "PATH_EDGE_COST_SENSITIVITY_36M_20260809_V1_FROZEN"
COST_BPS = (37, 25, 15, 10, 5, 0)
PHASES = ("DEV", "VAL", "HOLDOUT", "EXTERNAL_A", "EXTERNAL_B")
PHASE_MIN_N = {
    "DEV": MIN_DEV,
    "VAL": MIN_VAL,
    "HOLDOUT": MIN_HOLDOUT,
    "EXTERNAL_A": MIN_EXTERNAL,
    "EXTERNAL_B": MIN_EXTERNAL,
}


class PathEdgeCostSensitivity36mAudit(PathEdgeDiscoveryBinance36mV3Audit):
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

    def _sens_stats(self, rows: list[dict[str, Any]], cost_bps: int) -> dict[str, Any]:
        c = float(cost_bps) / 10000.0
        vals = [float(r["gross_r"]) - c for r in rows]
        wins = [v for v in vals if v > 0.0]
        losses = [v for v in vals if v < 0.0]
        gp = sum(wins)
        gl = abs(sum(losses))
        pair_pnl: dict[str, float] = {}
        for row, value in zip(rows, vals):
            pair = str(row["pair"])
            pair_pnl[pair] = pair_pnl.get(pair, 0.0) + self.notional * value
        positive = sum(v for v in pair_pnl.values() if v > 0.0)
        top = max([v for v in pair_pnl.values() if v > 0.0], default=0.0)
        return {
            "n": len(rows),
            "wr": 100.0 * len(wins) / len(rows) if rows else 0.0,
            "pf": gp / gl if gl else (999.0 if gp else 0.0),
            "exp": statistics.mean(vals) if vals else 0.0,
            "net_eur": self.notional * sum(vals),
            "top_pair_share": top / positive if positive > 0.0 else 1.0,
        }

    @staticmethod
    def _bootstrap_sens(rows: list[dict[str, Any]], cost_bps: int, seed: int, nboot: int = 3000) -> tuple[float, float]:
        c = float(cost_bps) / 10000.0
        vals = [float(r["gross_r"]) - c for r in rows]
        if len(vals) < 10:
            return (0.0, 0.0)
        rng = random.Random(seed)
        n = len(vals)
        means = [sum(vals[rng.randrange(n)] for _ in range(n)) / n for _ in range(nboot)]
        means.sort()
        return means[int(0.025 * nboot)], means[min(nboot - 1, int(0.975 * nboot))]

    def run(self) -> dict[str, Any]:
        if self.already_done():
            return {"skipped": True, "version": AUDIT_VERSION}

        coverage: list[dict[str, Any]] = []
        raw_by_pair: dict[str, Any] = {}
        for symbol in SYMBOLS:
            raw, info = self._fetch_symbol(symbol)
            raw_by_pair[PAIR_NAMES[symbol]] = raw
            coverage.append(info)

        prepared: dict[tuple[str, str], Any] = {}
        for pair, raw in raw_by_pair.items():
            for tf in TIMEFRAMES:
                prepared[(pair, tf)] = self._prepare36(raw, tf)
        event_names = list(next(iter(prepared.values()))[1].keys())

        families: list[dict[str, Any]] = []
        for tf in TIMEFRAMES:
            for event_name in event_names:
                best: dict[str, Any] | None = None

                # Reproduce the original V3 selection exactly: DEV only, current
                # configured economic costs, n >= MIN_DEV, then max net exp/PF/n.
                for barrier_name, tp_pct, sl_pct in BARRIERS:
                    for hold_h in MAX_HOLDS_H:
                        dev_rows: list[dict[str, Any]] = []
                        direction = "LONG"
                        for pair in PAIR_NAMES.values():
                            x, events = prepared[(pair, tf)]
                            mask, direction = events[event_name]
                            dev_rows.extend(self._simulate_pair_candidate(
                                x, mask, direction, pair, tf, event_name,
                                tp_pct, sl_pct, hold_h, phase_filter="DEV",
                            ))
                        dev = self._stats(dev_rows)
                        if dev["n"] < MIN_DEV:
                            continue
                        candidate = {
                            "id": f"{tf}_{event_name}_{barrier_name}_{hold_h}h",
                            "tf": tf,
                            "event": event_name,
                            "direction": direction,
                            "barrier": barrier_name,
                            "tp_pct": tp_pct,
                            "sl_pct": sl_pct,
                            "hold_h": hold_h,
                            "selection_dev": dev,
                        }
                        if best is None or (dev["exp"], dev["pf"], dev["n"]) > (
                            best["selection_dev"]["exp"],
                            best["selection_dev"]["pf"],
                            best["selection_dev"]["n"],
                        ):
                            best = candidate

                if best is None:
                    continue

                all_rows, direction = self._run_frozen_candidate(
                    prepared,
                    tf,
                    event_name,
                    best["tp_pct"],
                    best["sl_pct"],
                    best["hold_h"],
                )
                best["direction"] = direction
                phase_rows = {phase: self._phase_rows(all_rows, phase) for phase in PHASES}
                gross_exp = {
                    phase: statistics.mean([float(r["gross_r"]) for r in rows]) if rows else 0.0
                    for phase, rows in phase_rows.items()
                }
                best["gross_exp_by_phase"] = gross_exp
                best["maximin_break_even_bps"] = 10000.0 * min(gross_exp.values())

                sensitivity: dict[str, Any] = {}
                for cost_bps in COST_BPS:
                    phase_stats = {
                        phase: self._sens_stats(rows, cost_bps)
                        for phase, rows in phase_rows.items()
                    }
                    positive_all = all(
                        phase_stats[phase]["n"] >= PHASE_MIN_N[phase]
                        and phase_stats[phase]["pf"] > 1.0
                        and phase_stats[phase]["exp"] > 0.0
                        for phase in PHASES
                    )
                    ext_b_ci = self._bootstrap_sens(
                        phase_rows["EXTERNAL_B"], cost_bps,
                        seed=520000 + len(families) * 10 + cost_bps,
                    ) if positive_all else (0.0, 0.0)
                    robust = bool(
                        positive_all
                        and ext_b_ci[0] > 0.0
                        and phase_stats["EXTERNAL_B"]["top_pair_share"] <= 0.70
                    )
                    sensitivity[str(cost_bps)] = {
                        "phases": phase_stats,
                        "positive_all": positive_all,
                        "external_b_ci": ext_b_ci,
                        "robust": robust,
                        "worst_exp": min(phase_stats[p]["exp"] for p in PHASES),
                    }
                best["sensitivity"] = sensitivity
                families.append(best)

        cost_summary: list[dict[str, Any]] = []
        for cost_bps in COST_BPS:
            key = str(cost_bps)
            positive = [f for f in families if f["sensitivity"][key]["positive_all"]]
            robust = [f for f in families if f["sensitivity"][key]["robust"]]
            best = max(
                families,
                key=lambda f: (
                    float(f["sensitivity"][key]["worst_exp"]),
                    float(f["maximin_break_even_bps"]),
                ),
                default=None,
            )
            cost_summary.append({
                "cost_bps": cost_bps,
                "positive_all": len(positive),
                "robust": len(robust),
                "best_id": best["id"] if best else None,
                "best_worst_exp": best["sensitivity"][key]["worst_exp"] if best else 0.0,
            })

        top_maximin = sorted(
            families,
            key=lambda f: float(f["maximin_break_even_bps"]),
            reverse=True,
        )[:8]
        zero_positive = [f for f in families if f["sensitivity"]["0"]["positive_all"]]
        zero_positive = sorted(
            zero_positive,
            key=lambda f: float(f["sensitivity"]["0"]["worst_exp"]),
            reverse=True,
        )[:6]

        summary = {
            "version": AUDIT_VERSION,
            "families": len(families),
            "raw_grid": len(event_names) * len(TIMEFRAMES) * len(BARRIERS) * len(MAX_HOLDS_H),
            "cost_bps": list(COST_BPS),
            "cost_summary": cost_summary,
            "top_maximin": top_maximin,
            "zero_positive": zero_positive,
            "coverage": coverage,
            "configured_nominal_bps": 10000.0 * (
                self.buy_fee + self.sell_fee + self.spread + 2.0 * self.slippage
            ),
        }
        self._save_marker(summary)
        return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        if s.get("skipped"):
            return ""
        lines = [
            "💸 <b>PATH EDGE · COST SENSITIVITY 36 MESI</b>",
            "Stesse 90 famiglie congelate · nessun ritocco a evento/TF/TP/SL/holding",
            f"Griglia di selezione DEV riprodotta: <b>{s.get('raw_grid',0)}</b> configurazioni",
            f"Costo nominale modello attuale: <b>{s.get('configured_nominal_bps',0):.1f} bps</b> round-trip",
            "Sensibilità: rendimento lordo delle stesse operazioni meno costo flat equivalente.",
            "",
            "📉 <b>Quante famiglie restano positive in TUTTE le 5 fasi?</b>",
        ]
        for row in s.get("cost_summary", []):
            lines.append(
                f"• {row['cost_bps']:>2} bps: positive {row['positive_all']}/{s.get('families',0)} · "
                f"robuste {row['robust']} · migliore fase-peggiore {100*row['best_worst_exp']:+.3f}%"
            )

        lines += ["", "🧱 <b>Top maximin · costo di break-even della fase peggiore</b>"]
        for f in s.get("top_maximin", [])[:6]:
            g = f["gross_exp_by_phase"]
            lines.append(
                f"• {f['tf']} · {f['event']} · TP {100*f['tp_pct']:.2f}% / SL {100*f['sl_pct']:.2f}% / {f['hold_h']}h · "
                f"BE {f['maximin_break_even_bps']:+.1f} bps · "
                f"gross DEV {100*g['DEV']:+.2f}% VAL {100*g['VAL']:+.2f}% H {100*g['HOLDOUT']:+.2f}% A {100*g['EXTERNAL_A']:+.2f}% B {100*g['EXTERNAL_B']:+.2f}%"
            )

        if s.get("zero_positive"):
            lines += ["", "🟢 <b>Positive in tutte le fasi a 0 bps</b>"]
            for f in s["zero_positive"][:4]:
                z = f["sensitivity"]["0"]
                lines.append(
                    f"• {f['tf']} · {f['event']} · worst Exp {100*z['worst_exp']:+.3f}% · BE {f['maximin_break_even_bps']:.1f} bps"
                )
        else:
            lines += [
                "",
                "❌ <b>NESSUNA famiglia è positiva in tutte le fasi nemmeno a 0 bps.</b>",
                "In questo caso il limite principale non è l'esecuzione: almeno una fase resta negativa già lorda.",
            ]

        lines += ["", "📥 <b>Dati Binance</b>"]
        for c in s.get("coverage", []):
            lines.append(f"• {c['pair']}: {c['bars']} barre · gap {c['missing_intervals']}")
        lines.append("Nota: i livelli 25/15/10/5/0 bps sono scenari diagnostici, non fee promesse o disponibili.")
        return "\n".join(lines)
