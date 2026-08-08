"""One-shot discovery audit for a new cost-aware strategy.

This deliberately does not alter Velez live rules. It starts from economic
viability (round-trip friction as a fraction of technical risk), then tests a
simple higher-timeframe trend + 15m momentum trigger with chronological holdout.
"""
from __future__ import annotations

from typing import Any
import json
import statistics

from project.velez_candidate_audit import VelezCandidateAudit

AUDIT_VERSION = "COST_AWARE_STRATEGY_DISCOVERY_V1_20260808"


class CostAwareStrategyAudit(VelezCandidateAudit):
    TARGETS = (1.0, 1.5, 2.0, 3.0)
    HORIZON = 48

    def __init__(self,*args:Any,notional_eur:float=25.0,buy_fee_rate:float=0.0016,sell_fee_rate:float=0.0016,spread_rate:float=0.0005,slippage_rate:float=0.0,**kwargs:Any)->None:
        super().__init__(*args,**kwargs)
        self.notional=float(notional_eur); self.buy_fee=float(buy_fee_rate); self.sell_fee=float(sell_fee_rate)
        self.spread=float(spread_rate); self.slippage=float(slippage_rate)
        self.round_trip=self.buy_fee+self.sell_fee+self.spread+2*self.slippage

    def already_done(self)->bool:
        self._ensure_marker()
        return bool(self.postgres.fetch_all("SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1",(AUDIT_VERSION,)))

    def _save_marker(self,s:dict[str,Any])->None:
        self.postgres.execute("INSERT INTO statistics.velez_candidate_audit_runs(version,summary) VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING",(AUDIT_VERSION,json.dumps(s,default=str)))

    def _net(self,direction:str,entry:float,exit_price:float)->float:
        qty=self.notional/entry
        gross=qty*((exit_price-entry) if direction=="LONG" else (entry-exit_price))
        fees=self.notional*self.buy_fee+qty*exit_price*self.sell_fee
        friction=qty*(entry+exit_price)*((self.spread/2.0)+self.slippage)
        return gross-fees-friction

    @staticmethod
    def _first(direction:str,entry:float,stop:float,future:Any,target_r:float)->tuple[str,float|None]:
        risk=abs(entry-stop); tp=entry+target_r*risk if direction=="LONG" else entry-target_r*risk
        for _,r in future.head(48).iterrows():
            hi,lo=float(r["high"]),float(r["low"])
            hit_tp=hi>=tp if direction=="LONG" else lo<=tp
            hit_sl=lo<=stop if direction=="LONG" else hi>=stop
            if hit_tp and hit_sl:return "AMBIGUOUS",None
            if hit_sl:return "SL",stop
            if hit_tp:return "TP",tp
        return "NONE",None

    def _rows(self)->list[dict[str,Any]]:
        out=[]
        for pair in self.pairs:
            d=self._frame(pair)
            if len(d)<260:continue
            # 1h context is derived only from completed groups of four 15m bars,
            # avoiding dependency on uneven historical 1h coverage.
            for i in range(220,len(d)-self.HORIZON):
                latest=d.iloc[i]; entry=float(latest["close"])
                if entry<=0:continue
                # completed 1h closes strictly before trigger bar
                prior=d.iloc[:i]
                closes=prior["close"].astype(float).to_numpy()
                if len(closes)<208:continue
                hourly=closes[(len(closes)%4):].reshape(-1,4)[:,-1]
                if len(hourly)<52:continue
                ema20h=float(__import__('pandas').Series(hourly).ewm(span=20,adjust=False).mean().iloc[-1])
                ema50h=float(__import__('pandas').Series(hourly).ewm(span=50,adjust=False).mean().iloc[-1])
                hclose=float(hourly[-1])
                long_ctx=hclose>ema20h>ema50h; short_ctx=hclose<ema20h<ema50h
                if not long_ctx and not short_ctx:continue
                direction="LONG" if long_ctx else "SHORT"
                prev=d.iloc[i-1]
                # 15m momentum trigger: close breaks prior bar in direction and has real body.
                rng=float(latest["high"])-float(latest["low"])
                body=abs(float(latest["close"])-float(latest["open"]))
                if rng<=0 or body/rng<0.45:continue
                if direction=="LONG":
                    trigger=float(latest["close"])>float(latest["open"]) and entry>float(prev["high"])
                    stop=float(d.iloc[i-8:i]["low"].min())-entry*0.0005
                else:
                    trigger=float(latest["close"])<float(latest["open"]) and entry<float(prev["low"])
                    stop=float(d.iloc[i-8:i]["high"].max())+entry*0.0005
                if not trigger:continue
                risk=abs(entry-stop); risk_pct=risk/entry
                if risk<=0 or risk_pct>0.06:continue
                cost_r=self.round_trip/risk_pct
                future=d.iloc[i+1:i+1+self.HORIZON]
                row={"time":latest["timestamp"],"pair":pair,"direction":direction,"entry":entry,"risk_pct":100*risk_pct,"cost_r":cost_r,"hour":int(latest["timestamp"].hour)}
                for t in self.TARGETS:
                    o,px=self._first(direction,entry,stop,future,t); row[f"t{t}_outcome"]=o; row[f"t{t}_pnl"]=self._net(direction,entry,px) if o in {"TP","SL"} and px is not None else None
                out.append(row)
        return sorted(out,key=lambda r:r["time"])

    @staticmethod
    def _stats(rows:list[dict[str,Any]],t:float)->dict[str,float]:
        resolved=[r for r in rows if r[f"t{t}_outcome"] in {"TP","SL"}]
        vals=[float(r[f"t{t}_pnl"]) for r in resolved if r[f"t{t}_pnl"] is not None]
        wins=sum(r[f"t{t}_outcome"]=="TP" for r in resolved)
        gp=sum(v for v in vals if v>0); gl=abs(sum(v for v in vals if v<0))
        return {"n":len(vals),"wr":100*wins/len(resolved) if resolved else 0.0,"pf":gp/gl if gl else (999.0 if gp else 0.0),"net":sum(vals),"exp":statistics.mean(vals) if vals else 0.0}

    def run(self)->dict[str,Any]:
        if self.already_done():return {"skipped":True,"version":AUDIT_VERSION}
        rows=self._rows(); cut=int(len(rows)*0.70); dev,hold=rows[:cut],rows[cut:]
        # Cost/R caps are hypotheses fixed in advance, not selected from holdout.
        caps=(0.15,0.20,0.25,0.30)
        result={}
        for cap in caps:
            dd=[r for r in dev if r["cost_r"]<=cap]; hh=[r for r in hold if r["cost_r"]<=cap]
            result[str(cap)]={str(t):{"dev":self._stats(dd,t),"hold":self._stats(hh,t)} for t in self.TARGETS}
        robust=[]
        for cap,xs in result.items():
            for t,x in xs.items():
                d,h=x["dev"],x["hold"]
                if d["n"]>=30 and h["n"]>=15 and d["pf"]>1 and h["pf"]>1 and d["exp"]>0 and h["exp"]>0:robust.append({"cap":cap,"target":t,"dev":d,"hold":h})
        robust.sort(key=lambda x:x["hold"]["exp"],reverse=True)
        s={"version":AUDIT_VERSION,"rows":len(rows),"dev":len(dev),"hold":len(hold),"round_trip_pct":100*self.round_trip,"risk_median":statistics.median([r["risk_pct"] for r in rows]) if rows else 0.0,"cost_r_median":statistics.median([r["cost_r"] for r in rows]) if rows else 0.0,"result":result,"robust":robust[:8]}
        self._save_marker(s);return s

    @staticmethod
    def format_report(s:dict[str,Any])->str:
        if s.get("skipped"):return ""
        lines=["🧱 <b>AUDIT NUOVA STRATEGIA · COST-AWARE</b>",f"Setup grezzi: <b>{s.get('rows',0)}</b> · sviluppo <b>{s.get('dev',0)}</b> · holdout <b>{s.get('hold',0)}</b>",f"Costo round-trip: <b>{s.get('round_trip_pct',0):.3f}%</b> · stop mediano: <b>{s.get('risk_median',0):.3f}%</b> · costo mediano: <b>{s.get('cost_r_median',0):.2f}R</b>","","📊 <b>Vincolo economico → risultati netti HOLD</b>"]
        for cap,xs in s.get("result",{}).items():
            lines.append(f"• costi max {float(cap):.0%} di 1R")
            for t,x in xs.items():
                h=x["hold"]; lines.append(f"  TP {t}R · n={h['n']} · WR {h['wr']:.1f}% · PF {h['pf']:.2f} · P&L €{h['net']:+.2f} · Exp €{h['exp']:+.3f}")
        lines += ["","✅ <b>Configurazioni positive sia DEV sia HOLD</b>"]
        if not s.get("robust"):lines.append("• Nessuna. Il nuovo ingresso non ha ancora un edge robusto dopo i costi.")
        else:
            for x in s["robust"]:lines.append(f"• costi≤{float(x['cap']):.0%}R · TP {x['target']}R · DEV PF {x['dev']['pf']:.2f} Exp €{x['dev']['exp']:+.3f} · HOLD n={x['hold']['n']} PF {x['hold']['pf']:.2f} Exp €{x['hold']['exp']:+.3f}")
        lines.append("\n⚠️ Ricerca diagnostica one-shot. Nuova logica separata da Velez; nessuna modifica al live. Trend 1h ricostruito solo da barre 15m precedenti, trigger momentum 15m, stop struttura ultime 8 barre. Nessun look-ahead.")
        return "\n".join(lines)
