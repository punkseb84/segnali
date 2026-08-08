"""One-shot hybrid audit: Velez trigger on 15m, stop/target scale from known 1h structure.
Diagnostic only; never changes live rules.
"""
from __future__ import annotations

from typing import Any
import json
import statistics
import pandas as pd

from project.velez_edge_validation_audit import VelezEdgeValidationAudit

AUDIT_VERSION = "VELEZ_15M_TRIGGER_1H_STRUCTURE_V1_20260808"


class Velez15mTrigger1hStructureAudit(VelezEdgeValidationAudit):
    TARGETS=(1.0,1.5,2.0)
    HORIZON=24

    def __init__(self,*args:Any,notional_eur:float=25.0,buy_fee_rate:float=0.0016,sell_fee_rate:float=0.0016,spread_rate:float=0.0005,slippage_rate:float=0.0,**kwargs:Any)->None:
        super().__init__(*args,**kwargs)
        self.notional=float(notional_eur); self.buy_fee=float(buy_fee_rate); self.sell_fee=float(sell_fee_rate)
        self.spread=float(spread_rate); self.slippage=float(slippage_rate)

    def already_done(self)->bool:
        self._ensure_marker()
        return bool(self.postgres.fetch_all("SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1",(AUDIT_VERSION,)))

    def _save_marker(self,summary:dict[str,Any])->None:
        self.postgres.execute("INSERT INTO statistics.velez_candidate_audit_runs(version,summary) VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING",(AUDIT_VERSION,json.dumps(summary,default=str)))

    def _frame_1h(self,pair:str)->pd.DataFrame:
        rows=self.postgres.fetch_all("""SELECT timestamp,open,high,low,close,volume FROM (
            SELECT timestamp,open,high,low,close,volume FROM market_data.ohlc
            WHERE LOWER(TRIM(exchange))=LOWER(TRIM(%s)) AND pair=%s AND timeframe='1h'
            ORDER BY timestamp DESC LIMIT 1800) q ORDER BY timestamp ASC""",(self.exchange,pair))
        d=pd.DataFrame(rows,columns=["timestamp","open","high","low","close","volume"])
        if d.empty:return d
        d["timestamp"]=pd.to_datetime(d["timestamp"],utc=True)
        for c in ("open","high","low","close","volume"):d[c]=pd.to_numeric(d[c],errors="coerce")
        return d.dropna().reset_index(drop=True)

    @staticmethod
    def _stop_1h(direction:str,entry:float,d1h:pd.DataFrame,signal_time:Any)->float|None:
        # Only fully closed 1h candles available at signal time; last 3 closed bars define structure.
        closed=d1h[d1h["timestamp"]+pd.Timedelta(hours=1) <= signal_time].tail(3)
        if len(closed)<3:return None
        if direction=="LONG": return float(closed["low"].min())-entry*0.0005
        return float(closed["high"].max())+entry*0.0005

    def _net(self,direction:str,entry:float,exit_price:float)->float:
        qty=self.notional/entry
        gross=qty*((exit_price-entry) if direction=="LONG" else (entry-exit_price))
        fees=self.notional*self.buy_fee + qty*exit_price*self.sell_fee
        friction=qty*(entry+exit_price)*((self.spread/2.0)+self.slippage)
        return gross-fees-friction

    @staticmethod
    def _first(direction:str,entry:float,stop:float,future:pd.DataFrame,target_r:float)->tuple[str,float|None]:
        risk=abs(entry-stop); tp=entry+target_r*risk if direction=="LONG" else entry-target_r*risk
        for _,r in future.head(24).iterrows():
            hi,lo=float(r["high"]),float(r["low"])
            tp_hit=hi>=tp if direction=="LONG" else lo<=tp
            sl_hit=lo<=stop if direction=="LONG" else hi>=stop
            if tp_hit and sl_hit:return "AMBIGUOUS",None
            if sl_hit:return "SL",stop
            if tp_hit:return "TP",tp
        return "NONE",None

    def _rows(self)->list[dict[str,Any]]:
        out=[]
        for pair in self.pairs:
            d=self._frame(pair); h=self._frame_1h(pair)
            if len(d)<230 or len(h)<20:continue
            start=max(205,self.pullback_bars+3); end=len(d)-24
            for i in range(start,end):
                latest,prev=d.iloc[i],d.iloc[i-1]; pull=d.iloc[i-self.pullback_bars:i]
                entry=float(latest["close"]); e20=float(latest["ema20"]); e20_2=float(d.iloc[i-2]["ema20"]); e200=float(latest["ema200"])
                long=entry>e20>e200 and e20>e20_2; short=entry<e20<e200 and e20<e20_2
                if not long and not short:continue
                direction="LONG" if long else "SHORT"
                if long:
                    ordered=all(float(pull.iloc[j]["high"])>=float(pull.iloc[j+1]["high"]) for j in range(len(pull)-1)); touched=bool((pull["low"]<=pull["ema20"]).any()); held=bool((pull["close"]>pull["ema200"]).all()); trigger=float(latest["close"])>float(latest["open"]) and float(latest["high"])>float(prev["high"]) and entry>float(prev["high"]); stop15=float(pull["low"].min())-entry*0.0005
                else:
                    ordered=all(float(pull.iloc[j]["low"])<=float(pull.iloc[j+1]["low"]) for j in range(len(pull)-1)); touched=bool((pull["high"]>=pull["ema20"]).any()); held=bool((pull["close"]<pull["ema200"]).all()); trigger=float(latest["close"])<float(latest["open"]) and float(latest["low"])<float(prev["low"]) and entry<float(prev["low"]); stop15=float(pull["high"].max())+entry*0.0005
                if not (ordered and touched and held and trigger):continue
                stop1h=self._stop_1h(direction,entry,h,latest["timestamp"])
                if stop1h is None:continue
                r15=abs(entry-stop15); r1h=abs(entry-stop1h)
                if r15<=0 or r1h<=0 or r15/entry>0.03 or r1h/entry>0.05:continue
                future=d.iloc[i+1:i+25]
                row={"time":latest["timestamp"],"pair":pair,"direction":direction,"entry":entry,"risk15_pct":100*r15/entry,"risk1h_pct":100*r1h/entry}
                for label,stop in (("15m",stop15),("1h",stop1h)):
                    for t in self.TARGETS:
                        outcome,px=self._first(direction,entry,stop,future,t); row[f"{label}_{t}_outcome"]=outcome
                        row[f"{label}_{t}_pnl"]=self._net(direction,entry,px) if outcome in {"TP","SL"} and px is not None else None
                out.append(row)
        return sorted(out,key=lambda r:r["time"])

    @staticmethod
    def _stats(rows:list[dict[str,Any]],label:str,t:float)->dict[str,float]:
        vals=[float(r[f"{label}_{t}_pnl"]) for r in rows if r.get(f"{label}_{t}_pnl") is not None]
        resolved=[r for r in rows if r.get(f"{label}_{t}_outcome") in {"TP","SL"}]
        tech_w=sum(r[f"{label}_{t}_outcome"]=="TP" for r in resolved)
        gp=sum(v for v in vals if v>0); gl=abs(sum(v for v in vals if v<0))
        return {"n":len(vals),"wr":100*tech_w/len(resolved) if resolved else 0.0,"pf":gp/gl if gl else (999.0 if gp else 0.0),"net":sum(vals),"exp":statistics.mean(vals) if vals else 0.0}

    def run(self)->dict[str,Any]:
        if self.already_done():return {"skipped":True,"version":AUDIT_VERSION}
        rows=self._rows(); cut=int(len(rows)*0.70); dev,hold=rows[:cut],rows[cut:]
        result={}
        for label in ("15m","1h"):
            result[label]={str(t):{"dev":self._stats(dev,label,t),"hold":self._stats(hold,label,t)} for t in self.TARGETS}
        summary={"version":AUDIT_VERSION,"rows":len(rows),"dev":len(dev),"hold":len(hold),"risk15":statistics.median([r["risk15_pct"] for r in rows]) if rows else 0.0,"risk1h":statistics.median([r["risk1h_pct"] for r in rows]) if rows else 0.0,"result":result}
        self._save_marker(summary); return summary

    @staticmethod
    def format_report(s:dict[str,Any])->str:
        if s.get("skipped"):return ""
        lines=["🔀 <b>AUDIT VELEZ · TRIGGER 15m / STRUTTURA 1h</b>",f"Setup confrontabili: <b>{s.get('rows',0)}</b> · sviluppo <b>{s.get('dev',0)}</b> · holdout <b>{s.get('hold',0)}</b>",f"Stop mediano 15m: <b>{s.get('risk15',0):.3f}%</b> · stop strutturale 1h: <b>{s.get('risk1h',0):.3f}%</b>","","📌 <b>Stop originale 15m</b>"]
        for t,x in s.get("result",{}).get("15m",{}).items():
            d,h=x["dev"],x["hold"]; lines.append(f"• TP {t}R · DEV PF {d['pf']:.2f} Exp €{d['exp']:+.3f} · HOLD n={h['n']} WR {h['wr']:.1f}% PF <b>{h['pf']:.2f}</b> P&L <b>€{h['net']:+.2f}</b> Exp <b>€{h['exp']:+.3f}</b>")
        lines += ["","🕐 <b>Stesso trigger 15m · stop strutturale 1h</b>"]
        for t,x in s.get("result",{}).get("1h",{}).items():
            d,h=x["dev"],x["hold"]; lines.append(f"• TP {t}R · DEV PF {d['pf']:.2f} Exp €{d['exp']:+.3f} · HOLD n={h['n']} WR {h['wr']:.1f}% PF <b>{h['pf']:.2f}</b> P&L <b>€{h['net']:+.2f}</b> Exp <b>€{h['exp']:+.3f}</b>")
        robust=[]
        for t,x in s.get("result",{}).get("1h",{}).items():
            if x["dev"]["pf"]>1 and x["hold"]["pf"]>1 and x["dev"]["exp"]>0 and x["hold"]["exp"]>0:robust.append(t)
        lines += ["",("✅ Struttura 1h positiva DEV+HOLD: <b>"+", ".join(robust)+"R</b>" if robust else "❌ Nessun target con stop 1h mantiene PF netto maggiore di 1 ed expectancy positiva sia DEV sia HOLD."),"","⚠️ One-shot diagnostico. Il trigger resta Velez 15m; lo stop 1h usa solo le ultime 3 candele orarie già completamente chiuse al momento del segnale. Nessuna modifica al live."]
        return "\n".join(lines)
