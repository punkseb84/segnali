"""One-shot Velez 15m structural-room economics audit.

Keeps the original technical entry/stop unchanged. Uses only information available
at entry to estimate room to the nearest prior structural level, then tests whether
risk/room segments survive Binance fees in both development and chronological holdout.
Diagnostic only; no live changes.
"""
from __future__ import annotations

from typing import Any, Callable
import math
import statistics

from project.velez_edge_validation_audit import VelezEdgeValidationAudit

AUDIT_VERSION = "VELEZ_STRUCTURE_ECONOMICS_V1_20260808"


class VelezStructureEconomicsAudit(VelezEdgeValidationAudit):
    LOOKBACK = 96
    TARGETS = (1.0, 1.5, 2.0)

    def __init__(self, *args: Any, notional_eur: float = 25.0,
                 buy_fee_rate: float = 0.0016, sell_fee_rate: float = 0.0016,
                 spread_rate: float = 0.0005, slippage_rate: float = 0.0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.notional = float(notional_eur)
        self.buy_fee = float(buy_fee_rate)
        self.sell_fee = float(sell_fee_rate)
        self.spread = float(spread_rate)
        self.slippage = float(slippage_rate)

    def already_done(self) -> bool:
        self._ensure_marker()
        return bool(self.postgres.fetch_all(
            "SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1",
            (AUDIT_VERSION,),
        ))

    def _save_marker(self, summary: dict[str, Any]) -> None:
        import json
        self.postgres.execute(
            "INSERT INTO statistics.velez_candidate_audit_runs(version,summary) VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING",
            (AUDIT_VERSION, json.dumps(summary, default=str)),
        )

    def _net_pnl(self, direction: str, entry: float, exit_price: float) -> float:
        qty = self.notional / entry
        gross = qty * ((exit_price-entry) if direction == "LONG" else (entry-exit_price))
        entry_fee = self.notional * self.buy_fee
        exit_notional = qty * exit_price
        exit_fee = exit_notional * self.sell_fee
        friction = qty * (entry + exit_price) * ((self.spread/2.0) + self.slippage)
        return gross - entry_fee - exit_fee - friction

    def _rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for pair in self.pairs:
            d = self._frame(pair)
            if len(d) < max(230, self.LOOKBACK + 5):
                continue
            start = max(205, self.pullback_bars + 3, self.LOOKBACK)
            end = len(d) - 24
            for i in range(start, end):
                latest, prev = d.iloc[i], d.iloc[i-1]
                pull = d.iloc[i-self.pullback_bars:i]
                e20, e20_2, e200 = float(latest["ema20"]), float(d.iloc[i-2]["ema20"]), float(latest["ema200"])
                entry = float(latest["close"])
                long = entry > e20 > e200 and e20 > e20_2
                short = entry < e20 < e200 and e20 < e20_2
                if not long and not short:
                    continue
                direction = "LONG" if long else "SHORT"
                if long:
                    ordered = all(float(pull.iloc[j]["high"]) >= float(pull.iloc[j+1]["high"]) for j in range(len(pull)-1))
                    touched = bool((pull["low"] <= pull["ema20"]).any())
                    held = bool((pull["close"] > pull["ema200"]).all())
                    trigger = float(latest["close"]) > float(latest["open"]) and float(latest["high"]) > float(prev["high"]) and entry > float(prev["high"])
                    stop = float(pull["low"].min()) - entry*0.0005
                else:
                    ordered = all(float(pull.iloc[j]["low"]) <= float(pull.iloc[j+1]["low"]) for j in range(len(pull)-1))
                    touched = bool((pull["high"] >= pull["ema20"]).any())
                    held = bool((pull["close"] < pull["ema200"]).all())
                    trigger = float(latest["close"]) < float(latest["open"]) and float(latest["low"]) < float(prev["low"]) and entry < float(prev["low"])
                    stop = float(pull["high"].max()) + entry*0.0005
                if not (ordered and touched and held and trigger):
                    continue
                risk = abs(entry-stop)
                if risk <= 0 or risk/entry > 0.03:
                    continue

                history = d.iloc[i-self.LOOKBACK:i]
                if direction == "LONG":
                    levels = [float(x) for x in history["high"] if float(x) > entry]
                    structure = min(levels) if levels else None
                    room_abs = (structure-entry) if structure is not None else None
                else:
                    levels = [float(x) for x in history["low"] if float(x) < entry]
                    structure = max(levels) if levels else None
                    room_abs = (entry-structure) if structure is not None else None
                room_pct = 100.0*room_abs/entry if room_abs is not None else None
                room_r = room_abs/risk if room_abs is not None else None

                future = d.iloc[i+1:i+25]
                row: dict[str, Any] = {
                    "pair":pair,"time":latest["timestamp"],"hour":int(latest["timestamp"].hour),
                    "direction":direction,"entry":entry,"stop":stop,"risk":risk,
                    "risk_pct":100.0*risk/entry,"room_pct":room_pct,"room_r":room_r,
                    "open_space":room_abs is None,
                }
                for target in self.TARGETS:
                    outcome = self._target_touch(direction, entry, risk, future, target)
                    row[f"t{target}"] = outcome
                    if outcome == "TP":
                        exit_price = entry + target*risk if direction == "LONG" else entry-target*risk
                        row[f"pnl{target}"] = self._net_pnl(direction,entry,exit_price)
                    elif outcome == "SL":
                        row[f"pnl{target}"] = self._net_pnl(direction,entry,stop)
                    else:
                        row[f"pnl{target}"] = None
                rows.append(row)
        return sorted(rows,key=lambda r:r["time"])

    @staticmethod
    def _econ(rows: list[dict[str, Any]], target: float) -> dict[str, float]:
        vals=[float(r[f"pnl{target}"]) for r in rows if r.get(f"pnl{target}") is not None]
        if not vals:
            return {"n":0,"pf":0.0,"net":0.0,"exp":0.0,"netpos":0.0}
        gp=sum(x for x in vals if x>0); gl=abs(sum(x for x in vals if x<0))
        return {"n":len(vals),"pf":gp/gl if gl>0 else (999.0 if gp>0 else 0.0),"net":sum(vals),
                "exp":statistics.mean(vals),"netpos":100.0*sum(x>0 for x in vals)/len(vals)}

    def run(self) -> dict[str, Any]:
        if self.already_done():
            return {"skipped":True,"version":AUDIT_VERSION}
        rows=self._rows(); cut=int(len(rows)*0.70); dev,hold=rows[:cut],rows[cut:]
        base={str(t):{"dev":self._econ(dev,t),"hold":self._econ(hold,t)} for t in self.TARGETS}

        # Thresholds are learned only on development data, then frozen on holdout.
        filters: list[tuple[str,Callable[[dict[str,Any]],bool]]] = []
        for key in ("risk_pct","room_pct","room_r"):
            vals=sorted(float(r[key]) for r in dev if r.get(key) is not None and math.isfinite(float(r[key])))
            if not vals: continue
            for q,label in ((0.33,"Q33"),(0.50,"Q50"),(0.67,"Q67")):
                idx=min(len(vals)-1,max(0,int((len(vals)-1)*q))); v=vals[idx]
                filters.append((f"{key} >= {v:.3f} ({label})",lambda r,k=key,v=v:r.get(k) is not None and float(r[k])>=v))
                filters.append((f"{key} <= {v:.3f} ({label})",lambda r,k=key,v=v:r.get(k) is not None and float(r[k])<=v))
        filters.append(("OPEN_SPACE",lambda r:bool(r.get("open_space"))))
        filters.append(("UTC 06-12",lambda r:6<=int(r["hour"])<12))

        tests=[]
        for name,fn in filters:
            d=[r for r in dev if fn(r)]; h=[r for r in hold if fn(r)]
            for t in self.TARGETS:
                ds,hs=self._econ(d,t),self._econ(h,t)
                if ds["n"]>=40 and hs["n"]>=15:
                    tests.append({"filter":name,"target":t,"dev":ds,"hold":hs})

        # Two-factor combinations are limited to economically meaningful room/risk gates.
        dev_room=[float(r["room_r"]) for r in dev if r.get("room_r") is not None]
        dev_risk=[float(r["risk_pct"]) for r in dev]
        if dev_room and dev_risk:
            room_med=statistics.median(dev_room); risk_med=statistics.median(dev_risk)
            combos=[
                (f"room_r>={room_med:.2f} & risk_pct>={risk_med:.3f}",lambda r,rm=room_med,rk=risk_med:r.get("room_r") is not None and float(r["room_r"])>=rm and float(r["risk_pct"])>=rk),
                (f"UTC06-12 & room_r>={room_med:.2f}",lambda r,rm=room_med:6<=int(r["hour"])<12 and r.get("room_r") is not None and float(r["room_r"])>=rm),
                (f"UTC06-12 & risk_pct>={risk_med:.3f}",lambda r,rk=risk_med:6<=int(r["hour"])<12 and float(r["risk_pct"])>=rk),
            ]
            for name,fn in combos:
                d=[r for r in dev if fn(r)]; h=[r for r in hold if fn(r)]
                for t in self.TARGETS:
                    ds,hs=self._econ(d,t),self._econ(h,t)
                    if ds["n"]>=30 and hs["n"]>=12:
                        tests.append({"filter":name,"target":t,"dev":ds,"hold":hs})

        tests.sort(key=lambda x:(x["hold"]["exp"],x["hold"]["pf"]),reverse=True)
        robust=[x for x in tests if x["dev"]["pf"]>1 and x["hold"]["pf"]>1 and x["dev"]["exp"]>0 and x["hold"]["exp"]>0]
        summary={
            "version":AUDIT_VERSION,"rows":len(rows),"dev":len(dev),"hold":len(hold),"base":base,
            "median_risk_pct":statistics.median([r["risk_pct"] for r in rows]) if rows else 0.0,
            "median_room_pct":statistics.median([r["room_pct"] for r in rows if r.get("room_pct") is not None]) if rows else 0.0,
            "median_room_r":statistics.median([r["room_r"] for r in rows if r.get("room_r") is not None]) if rows else 0.0,
            "open_space":sum(bool(r.get("open_space")) for r in rows),"robust":robust[:10],"best":tests[:10],
        }
        self._save_marker(summary); return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        if s.get("skipped"): return ""
        lines=["🏗 <b>AUDIT VELEZ · SPAZIO STRUTTURALE NETTO</b>",
               f"Setup: <b>{s.get('rows',0)}</b> · sviluppo <b>{s.get('dev',0)}</b> · holdout <b>{s.get('hold',0)}</b>",
               f"Stop mediano: <b>{s.get('median_risk_pct',0):.3f}%</b> · spazio strutturale mediano: <b>{s.get('median_room_pct',0):.3f}%</b> ≈ <b>{s.get('median_room_r',0):.2f}R</b>",
               f"Open space (nessun livello nelle 96 barre precedenti): <b>{s.get('open_space',0)}</b>","",
               "📊 <b>Baseline netta</b>"]
        for t,x in s.get("base",{}).items():
            d,h=x["dev"],x["hold"]
            lines.append(f"• TP {t}R · DEV n={d['n']} PF <b>{d['pf']:.2f}</b> Exp <b>€{d['exp']:+.3f}</b> · HOLD n={h['n']} PF <b>{h['pf']:.2f}</b> Exp <b>€{h['exp']:+.3f}</b>")
        lines += ["", "✅ <b>Filtri net-positive sia DEV sia HOLD</b>"]
        if not s.get("robust"):
            lines.append("• Nessuna combinazione testata mantiene PF netto >1 ed expectancy € positiva in entrambi i periodi.")
        else:
            for x in s["robust"]:
                d,h=x["dev"],x["hold"]
                lines.append(f"• {x['filter']} · TP {x['target']:.1f}R · DEV n={d['n']} PF {d['pf']:.2f} Exp €{d['exp']:+.3f} · HOLD n={h['n']} PF {h['pf']:.2f} Exp €{h['exp']:+.3f}")
        lines += ["", "🔎 <b>Migliori nel solo holdout</b>"]
        for x in s.get("best",[])[:5]:
            h=x["hold"]
            lines.append(f"• {x['filter']} · TP {x['target']:.1f}R · n={h['n']} · PF {h['pf']:.2f} · P&L €{h['net']:+.2f} · Exp €{h['exp']:+.3f}")
        lines.append("\n⚠️ One-shot diagnostico. Livelli strutturali ricavati esclusivamente dalle 96 barre precedenti all'ingresso; nessun look-ahead e nessuna modifica al live.")
        return "\n".join(lines)
