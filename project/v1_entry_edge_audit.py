"""Diagnostic V1 entry-edge audit with chronological holdout validation.

Uses only information available at signal time. It does not modify the live bot.
"""
from __future__ import annotations

from typing import Any, Callable
import statistics

from project.v1_historical_audit import V1HistoricalAudit, Trade, SYMBOLS


class V1EntryEdgeAudit:
    def __init__(self, audit: V1HistoricalAudit, logger: Any) -> None:
        self.audit = audit
        self.logger = logger

    @staticmethod
    def _pf(pnls: list[float]) -> float:
        gp = sum(x for x in pnls if x > 0)
        gl = abs(sum(x for x in pnls if x < 0))
        return gp / gl if gl > 0 else (999.0 if gp > 0 else 0.0)

    @staticmethod
    def _stats(rows: list[dict[str, Any]]) -> dict[str, float]:
        pnls = [float(x["pnl"]) for x in rows]
        return {
            "n": len(rows),
            "net": sum(pnls),
            "pf": V1EntryEdgeAudit._pf(pnls),
            "wr": (100.0 * sum(1 for x in pnls if x > 0) / len(pnls)) if pnls else 0.0,
            "exp": statistics.mean(pnls) if pnls else 0.0,
        }

    def _feature_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for pair in SYMBOLS:
            trades, _ = self.audit._signals_for_pair(pair)
            f15 = self.audit._indicators(self.audit._frame(pair, "15m"))
            f1h = self.audit._indicators(self.audit._frame(pair, "1h"))
            if f15.empty:
                continue
            for t in trades:
                cur = f15[f15["timestamp"] == t.time]
                if cur.empty:
                    continue
                c = cur.iloc[-1]
                tr = self.audit._align_trend(t.time, f1h)
                if tr is None:
                    continue
                future = f15[f15["timestamp"] > t.time]
                resolved = self.audit._resolve_fixed(t, future, 1.5, stop_first=True)
                if resolved.outcome not in {"TP", "SL"}:
                    continue
                pnl = self.audit._net_eur(resolved)
                atr = float(c["atr14"]) if float(c["atr14"]) > 0 else 1.0
                trend_atr = float(tr["atr14"]) if float(tr.get("atr14", 0.0) or 0.0) > 0 else 1.0
                ema20, ema50, ema200, close = map(float, (c["ema20"], c["ema50"], c["ema200"], c["close"]))
                volume_avg = float(c["volume_avg20"]) if float(c["volume_avg20"]) > 0 else 1.0
                sign = 1.0 if t.direction == "LONG" else -1.0
                rows.append({
                    "time": t.time, "pair": pair, "direction": t.direction, "pnl": pnl,
                    "rsi": float(c["rsi14"]),
                    "volume_ratio": float(c["volume"]) / volume_avg,
                    "atr_pct": 100.0 * atr / close,
                    "ema20_50_atr": sign * (ema20 - ema50) / atr,
                    "ema50_200_atr": sign * (ema50 - ema200) / atr,
                    "entry_ema20_atr": sign * (close - ema20) / atr,
                    "macd_atr": sign * float(c["macd_hist"]) / atr,
                    "trend200_atr": sign * (float(tr["close"]) - float(tr["ema200"])) / trend_atr,
                    "hour": int(t.time.hour),
                })
        return sorted(rows, key=lambda x: x["time"])

    @staticmethod
    def _quantile(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        pos = (len(s) - 1) * q
        lo, hi = int(pos), min(int(pos) + 1, len(s) - 1)
        frac = pos - lo
        return s[lo] * (1 - frac) + s[hi] * frac

    def run(self) -> dict[str, Any]:
        self.logger.info("V1_ENTRY_EDGE_AUDIT_START")
        rows = self._feature_rows()
        if len(rows) < 30:
            return {"rows": len(rows), "train": 0, "test": 0, "candidates": [], "validated": []}

        cut = max(1, int(len(rows) * 0.70))
        train, test = rows[:cut], rows[cut:]
        features = ["rsi", "volume_ratio", "atr_pct", "ema20_50_atr", "ema50_200_atr", "entry_ema20_atr", "macd_atr", "trend200_atr"]

        candidates: list[dict[str, Any]] = []
        for feature in features:
            vals = [float(x[feature]) for x in train]
            for q in (0.25, 0.50, 0.75):
                threshold = self._quantile(vals, q)
                for op in ("ge", "le"):
                    pred: Callable[[dict[str, Any]], bool] = (lambda r, f=feature, th=threshold: float(r[f]) >= th) if op == "ge" else (lambda r, f=feature, th=threshold: float(r[f]) <= th)
                    tr = [x for x in train if pred(x)]
                    te = [x for x in test if pred(x)]
                    if len(tr) < 15 or len(te) < 6:
                        continue
                    ts, vs = self._stats(tr), self._stats(te)
                    candidates.append({"label": f"{feature} {'≥' if op=='ge' else '≤'} {threshold:.3f}", "train": ts, "test": vs})

        # Categorical checks that are known before entry.
        for direction in ("LONG", "SHORT"):
            tr = [x for x in train if x["direction"] == direction]
            te = [x for x in test if x["direction"] == direction]
            if len(tr) >= 15 and len(te) >= 6:
                candidates.append({"label": direction, "train": self._stats(tr), "test": self._stats(te)})

        for start in (0, 6, 12, 18):
            end = start + 6
            tr = [x for x in train if start <= x["hour"] < end]
            te = [x for x in test if start <= x["hour"] < end]
            if len(tr) >= 12 and len(te) >= 5:
                candidates.append({"label": f"UTC {start:02d}-{end:02d}", "train": self._stats(tr), "test": self._stats(te)})

        # A filter is considered provisionally validated only if it is profitable in both periods.
        validated = [c for c in candidates if c["train"]["pf"] > 1.0 and c["train"]["net"] > 0 and c["test"]["pf"] > 1.0 and c["test"]["net"] > 0]
        validated.sort(key=lambda c: (c["test"]["pf"], c["test"]["net"], c["test"]["n"]), reverse=True)
        candidates.sort(key=lambda c: (c["test"]["pf"], c["test"]["net"]), reverse=True)

        summary = {
            "rows": len(rows), "train": len(train), "test": len(test),
            "baseline_train": self._stats(train), "baseline_test": self._stats(test),
            "candidates": candidates[:8], "validated": validated[:8],
        }
        self.logger.warning("V1_ENTRY_EDGE_AUDIT_SUMMARY %s", summary)
        return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        bt, bv = s.get("baseline_train", {}), s.get("baseline_test", {})
        lines = [
            "🧭 <b>AUDIT V1 · EDGE INGRESSI</b>",
            f"Trade risolti: <b>{s.get('rows',0)}</b> · sviluppo <b>{s.get('train',0)}</b> · verifica <b>{s.get('test',0)}</b>",
            f"Baseline sviluppo: PF <b>{bt.get('pf',0):.2f}</b> · P&L <b>€{bt.get('net',0):+.2f}</b> · WR net+ <b>{bt.get('wr',0):.1f}%</b>",
            f"Baseline verifica: PF <b>{bv.get('pf',0):.2f}</b> · P&L <b>€{bv.get('net',0):+.2f}</b> · WR net+ <b>{bv.get('wr',0):.1f}%</b>",
            "",
        ]
        val = s.get("validated", [])
        if val:
            lines.append("✅ <b>Filtri preliminarmente validati in entrambi i periodi</b>")
            for c in val:
                tr, te = c["train"], c["test"]
                lines.append(f"• {c['label']} · DEV n={tr['n']} PF {tr['pf']:.2f} €{tr['net']:+.2f} · HOLDOUT n={te['n']} PF <b>{te['pf']:.2f}</b> €<b>{te['net']:+.2f}</b>")
        else:
            lines.append("❌ <b>Nessun filtro singolo supera PF 1 e P&L positivo sia in sviluppo sia in verifica.</b>")
        lines += ["", "🔎 <b>Migliori segnali nel solo holdout</b>"]
        for c in s.get("candidates", [])[:5]:
            te = c["test"]
            lines.append(f"• {c['label']} · n={te['n']} · PF {te['pf']:.2f} · P&L €{te['net']:+.2f} · WR {te['wr']:.1f}%")
        lines.append("\n⚠️ Solo diagnostica. Le soglie sono derivate dal periodo iniziale; il periodo finale non viene usato per scegliere le soglie.")
        return "\n".join(lines)
