"""One-shot validation audit for qualified Velez 15m setups.
Measures 1.5R/2R before -1R and validates simple pre-entry segments with chronological holdout.
Diagnostic only; no live rule changes.
"""
from __future__ import annotations

from typing import Any
import statistics

from project.velez_candidate_audit import VelezCandidateAudit

AUDIT_VERSION = "VELEZ_TARGET_EDGE_VALIDATION_V1_20260808"


class VelezEdgeValidationAudit(VelezCandidateAudit):
    TARGETS = (1.0, 1.5, 2.0)
    HORIZON = 24

    def already_done(self) -> bool:
        self._ensure_marker()
        rows = self.postgres.fetch_all("SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1", (AUDIT_VERSION,))
        return bool(rows)

    def _save_marker(self, summary: dict[str, Any]) -> None:
        import json
        self.postgres.execute(
            "INSERT INTO statistics.velez_candidate_audit_runs(version,summary) VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING",
            (AUDIT_VERSION, json.dumps(summary, default=str)),
        )

    @staticmethod
    def _target_touch(direction: str, entry: float, risk: float, future: Any, target_r: float) -> str:
        tp = entry + target_r*risk if direction == "LONG" else entry - target_r*risk
        sl = entry - risk if direction == "LONG" else entry + risk
        for _, r in future.head(24).iterrows():
            hi, lo = float(r["high"]), float(r["low"])
            hit_tp = hi >= tp if direction == "LONG" else lo <= tp
            hit_sl = lo <= sl if direction == "LONG" else hi >= sl
            if hit_tp and hit_sl:
                return "AMBIGUOUS"
            if hit_sl:
                return "SL"
            if hit_tp:
                return "TP"
        return "NONE"

    def _qualified(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for pair in self.pairs:
            d = self._frame(pair)
            if len(d) < 230:
                continue
            start = max(205, self.pullback_bars + 3)
            end = len(d) - self.HORIZON
            for i in range(start, end):
                latest, prev = d.iloc[i], d.iloc[i-1]
                pull = d.iloc[i-self.pullback_bars:i]
                e20, e20_2, e200 = float(latest["ema20"]), float(d.iloc[i-2]["ema20"]), float(latest["ema200"])
                close = float(latest["close"])
                long = close > e20 > e200 and e20 > e20_2
                short = close < e20 < e200 and e20 < e20_2
                if not long and not short:
                    continue
                direction = "LONG" if long else "SHORT"
                if long:
                    ordered = all(float(pull.iloc[j]["high"]) >= float(pull.iloc[j+1]["high"]) for j in range(len(pull)-1))
                    touched = bool((pull["low"] <= pull["ema20"]).any())
                    held = bool((pull["close"] > pull["ema200"]).all())
                    trigger = float(latest["close"]) > float(latest["open"]) and float(latest["high"]) > float(prev["high"]) and close > float(prev["high"])
                    stop = float(pull["low"].min()) - close*0.0005
                else:
                    ordered = all(float(pull.iloc[j]["low"]) <= float(pull.iloc[j+1]["low"]) for j in range(len(pull)-1))
                    touched = bool((pull["high"] >= pull["ema20"]).any())
                    held = bool((pull["close"] < pull["ema200"]).all())
                    trigger = float(latest["close"]) < float(latest["open"]) and float(latest["low"]) < float(prev["low"]) and close < float(prev["low"])
                    stop = float(pull["high"].max()) + close*0.0005
                if not (ordered and touched and held and trigger):
                    continue
                risk = abs(close-stop)
                if risk <= 0 or risk/close > 0.03:
                    continue
                future = d.iloc[i+1:i+25]
                row = {
                    "pair": pair, "time": latest["timestamp"], "direction": direction,
                    "risk_pct":100*risk/close,
                    "slope":10000*abs(e20/e20_2-1) if e20_2 else 0,
                    "sep":100*abs(e20/e200-1) if e200 else 0,
                    "hour":int(latest["timestamp"].hour),
                }
                for target in self.TARGETS:
                    row[f"t{target}"] = self._target_touch(direction, close, risk, future, target)
                rows.append(row)
        return sorted(rows, key=lambda x: x["time"])

    @staticmethod
    def _stats(rows: list[dict[str, Any]], target: float) -> dict[str, float]:
        resolved = [r for r in rows if r[f"t{target}"] in {"TP","SL"}]
        wins = sum(r[f"t{target}"] == "TP" for r in resolved)
        wr = wins/len(resolved) if resolved else 0.0
        expectancy_r = wr*target - (1-wr)
        pf_r = (wins*target)/(len(resolved)-wins) if len(resolved)>wins else (999.0 if wins else 0.0)
        return {"n":len(rows),"resolved":len(resolved),"wr":100*wr,"exp_r":expectancy_r,"pf_r":pf_r}

    def run(self) -> dict[str, Any]:
        if self.already_done():
            return {"skipped":True,"version":AUDIT_VERSION}
        rows = self._qualified()
        cut = int(len(rows)*0.70)
        dev, hold = rows[:cut], rows[cut:]
        base = {str(t):{"dev":self._stats(dev,t),"hold":self._stats(hold,t)} for t in self.TARGETS}

        # Thresholds are derived only from development medians, then frozen for holdout.
        filters: list[tuple[str, Any]] = []
        for key in ("risk_pct","slope","sep"):
            vals=[float(r[key]) for r in dev]
            if vals:
                med=statistics.median(vals)
                filters += [(f"{key} <= {med:.3f}", lambda r,k=key,m=med: float(r[k])<=m),
                            (f"{key} >= {med:.3f}", lambda r,k=key,m=med: float(r[k])>=m)]
        filters += [("LONG",lambda r:r["direction"]=="LONG"),("SHORT",lambda r:r["direction"]=="SHORT")]
        for a,b in ((0,6),(6,12),(12,18),(18,24)):
            filters.append((f"UTC {a:02d}-{b:02d}",lambda r,a=a,b=b:a<=r["hour"]<b))

        validated=[]
        for name,fn in filters:
            d=[r for r in dev if fn(r)]; h=[r for r in hold if fn(r)]
            for t in (1.5,2.0):
                ds,hs=self._stats(d,t),self._stats(h,t)
                if ds["resolved"]>=50 and hs["resolved"]>=20:
                    validated.append({"filter":name,"target":t,"dev":ds,"hold":hs})
        validated.sort(key=lambda x:(x["hold"]["exp_r"],x["hold"]["pf_r"]),reverse=True)
        robust=[x for x in validated if x["dev"]["exp_r"]>0 and x["hold"]["exp_r"]>0 and x["dev"]["pf_r"]>1 and x["hold"]["pf_r"]>1]
        summary={"version":AUDIT_VERSION,"qualified":len(rows),"dev":len(dev),"hold":len(hold),"base":base,"robust":robust[:8],"best":validated[:8]}
        self._save_marker(summary)
        return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        if s.get("skipped"): return ""
        lines=["🧪 <b>AUDIT VELEZ · TARGET E HOLDOUT</b>",f"Setup qualificati: <b>{s.get('qualified',0)}</b> · sviluppo <b>{s.get('dev',0)}</b> · verifica <b>{s.get('hold',0)}</b>","","🎯 <b>Baseline · target prima di -1R · 24 barre</b>"]
        for t,x in s.get("base",{}).items():
            d,h=x["dev"],x["hold"]
            lines.append(f"• +{t}R: DEV WR <b>{d['wr']:.1f}%</b> PF-R <b>{d['pf_r']:.2f}</b> Exp <b>{d['exp_r']:+.3f}R</b> · HOLD WR <b>{h['wr']:.1f}%</b> PF-R <b>{h['pf_r']:.2f}</b> Exp <b>{h['exp_r']:+.3f}R</b>")
        lines += ["", "✅ <b>Filtri positivi sia DEV sia HOLD</b>"]
        if not s.get("robust"):
            lines.append("• Nessun filtro semplice supera PF-R 1 ed expectancy positiva in entrambi i periodi.")
        else:
            for x in s["robust"]:
                d,h=x["dev"],x["hold"]
                lines.append(f"• {x['filter']} · +{x['target']:.1f}R · DEV n={d['resolved']} PF {d['pf_r']:.2f} Exp {d['exp_r']:+.2f}R · HOLD n={h['resolved']} PF {h['pf_r']:.2f} Exp {h['exp_r']:+.2f}R")
        lines += ["", "🔎 <b>Migliori nel solo holdout</b>"]
        for x in s.get("best",[])[:5]:
            h=x["hold"]
            lines.append(f"• {x['filter']} · +{x['target']:.1f}R · n={h['resolved']} · WR {h['wr']:.1f}% · PF-R {h['pf_r']:.2f} · Exp {h['exp_r']:+.3f}R")
        lines.append("\n⚠️ Diagnostico one-shot. Soglie ricavate solo sul 70% iniziale e congelate sul 30% finale. Nessuna modifica al live; PF-R/expectancy sono tecnici prima dei costi.")
        return "\n".join(lines)
