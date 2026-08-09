"""One-shot multi-timeframe crypto edge discovery audit.

Research-only. It searches a small, predeclared family grid on stored 15m OHLC,
resampled to 15m/30m/1h/2h/4h. Candidate selection uses DEV only, one champion
is chosen after VALIDATION, and TEST is opened once for that champion. Costs are
included everywhere. No live trading rules are modified.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json
import math
import random
import statistics

import pandas as pd

from project.velez_candidate_audit import VelezCandidateAudit

AUDIT_VERSION = "EDGE_DISCOVERY_V2_20260809"
TIMEFRAMES = ("15m", "30m", "1h", "2h", "4h")
RULES = {"15m":"15min", "30m":"30min", "1h":"1h", "2h":"2h", "4h":"4h"}
MAX_HOLD = {"15m":48, "30m":24, "1h":12, "2h":6, "4h":3}  # 12h maximum hold
STOP_ATR = 2.0
MAX_RISK_PCT = 0.08
DEV_FRAC = 0.50
VAL_FRAC = 0.25


@dataclass(frozen=True)
class Spec:
    family: str
    timeframe: str
    target_r: float
    p1: float = 0.0
    p2: float = 0.0

    @property
    def key(self) -> str:
        if self.family == "breakout":
            body = f"BO{int(self.p1)}"
        elif self.family == "pullback":
            body = "PB_EMA20"
        elif self.family == "momentum":
            body = f"MOM{int(self.p1)}_{self.p2:.1f}ATR"
        else:
            body = f"MR_RSI{int(self.p1)}"
        return f"{self.timeframe}:{body}:TP{self.target_r:.1f}R:SL{STOP_ATR:.1f}ATR"


class EdgeDiscoveryV2Audit(VelezCandidateAudit):
    def __init__(self,*args:Any,notional_eur:float=25.0,buy_fee_rate:float=.0016,sell_fee_rate:float=.0016,spread_rate:float=.0005,slippage_rate:float=0.0,**kwargs:Any)->None:
        super().__init__(*args,**kwargs)
        self.notional=float(notional_eur);self.buy_fee=float(buy_fee_rate);self.sell_fee=float(sell_fee_rate);self.spread=float(spread_rate);self.slippage=float(slippage_rate)

    def already_done(self)->bool:
        self._ensure_marker()
        return bool(self.postgres.fetch_all("SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1",(AUDIT_VERSION,)))

    def _save_marker(self,s:dict[str,Any])->None:
        self.postgres.execute("INSERT INTO statistics.velez_candidate_audit_runs(version,summary) VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING",(AUDIT_VERSION,json.dumps(s,default=str)))

    @staticmethod
    def _specs()->list[Spec]:
        out=[]
        for tf in TIMEFRAMES:
            for n in (20,40):
                for r in (1.5,2.0):out.append(Spec("breakout",tf,r,float(n),0.0))
            for r in (1.5,2.0):out.append(Spec("pullback",tf,r))
            for m,z in ((4,1.5),(8,2.0)):
                for r in (1.5,2.0):out.append(Spec("momentum",tf,r,float(m),float(z)))
            for rsi in (25,30):
                for r in (1.0,1.5):out.append(Spec("mean_reversion",tf,r,float(rsi),0.0))
        return out

    @staticmethod
    def _resample(d:pd.DataFrame,tf:str)->pd.DataFrame:
        x=d[["timestamp","open","high","low","close","volume"]].copy().sort_values("timestamp")
        if tf!="15m":
            x=x.set_index("timestamp").resample(RULES[tf],origin="epoch",label="left",closed="left").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna().reset_index()
        x=x.reset_index(drop=True)
        c=x["close"].astype(float);h=x["high"].astype(float);l=x["low"].astype(float)
        x["ema20"]=c.ewm(span=20,adjust=False).mean();x["ema50"]=c.ewm(span=50,adjust=False).mean();x["ema200"]=c.ewm(span=200,adjust=False).mean()
        prev=c.shift(1);tr=pd.concat([(h-l).abs(),(h-prev).abs(),(l-prev).abs()],axis=1).max(axis=1)
        x["atr"]=tr.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
        delta=c.diff();gain=delta.clip(lower=0);loss=(-delta.clip(upper=0));ag=gain.ewm(alpha=1/14,adjust=False,min_periods=14).mean();al=loss.ewm(alpha=1/14,adjust=False,min_periods=14).mean();rs=ag/al.replace(0,float("nan"));x["rsi"]=100-(100/(1+rs))
        x["bb_mid"]=c.rolling(20).mean();sd=c.rolling(20).std(ddof=0);x["bb_lo"]=x["bb_mid"]-2*sd;x["bb_hi"]=x["bb_mid"]+2*sd
        x["vol_ma20"]=x["volume"].rolling(20).mean()
        return x

    @staticmethod
    def _signal(d:pd.DataFrame,i:int,s:Spec)->str|None:
        r=d.iloc[i];p=d.iloc[i-1]
        if any(pd.isna(r[k]) for k in ("atr","ema20","ema50","ema200","rsi","bb_lo","bb_hi","vol_ma20")):return None
        c=float(r["close"]);atr=float(r["atr"])
        if atr<=0 or c<=0:return None
        if s.family=="breakout":
            n=int(s.p1)
            if i<n+1:return None
            prev_hi=float(d["high"].iloc[i-n:i].max());prev_lo=float(d["low"].iloc[i-n:i].min())
            if c>prev_hi and r["ema50"]>r["ema200"]:return "LONG"
            if c<prev_lo and r["ema50"]<r["ema200"]:return "SHORT"
        elif s.family=="pullback":
            if r["ema20"]>r["ema50"]>r["ema200"] and float(r["low"])<=float(r["ema20"]) and c>float(r["ema20"]) and c>float(r["open"]):return "LONG"
            if r["ema20"]<r["ema50"]<r["ema200"] and float(r["high"])>=float(r["ema20"]) and c<float(r["ema20"]) and c<float(r["open"]):return "SHORT"
        elif s.family=="momentum":
            m=int(s.p1)
            if i<m:return None
            norm=(c-float(d.iloc[i-m]["close"]))/atr
            liquid=float(r["volume"])>=float(r["vol_ma20"])
            if liquid and norm>=s.p2 and r["ema20"]>r["ema50"]:return "LONG"
            if liquid and norm<=-s.p2 and r["ema20"]<r["ema50"]:return "SHORT"
        else:
            lo=s.p1;hi=100-s.p1
            if float(r["rsi"])<=lo and c<float(r["bb_lo"]) and r["ema50"]>r["ema200"]:return "LONG"
            if float(r["rsi"])>=hi and c>float(r["bb_hi"]) and r["ema50"]<r["ema200"]:return "SHORT"
        return None

    def _pnl(self,direction:str,entry:float,exit_px:float)->tuple[float,float]:
        qty=self.notional/entry;gross=qty*((exit_px-entry) if direction=="LONG" else (entry-exit_px));exit_notional=qty*exit_px
        costs=self.notional*self.buy_fee+exit_notional*self.sell_fee+(self.notional+exit_notional)*(self.spread/2+self.slippage)
        return gross,gross-costs

    def _trades(self,d:pd.DataFrame,pair:str,s:Spec)->list[dict[str,Any]]:
        out=[];i=210;n=len(d);hold=MAX_HOLD[s.timeframe]
        while i<n-2:
            side=self._signal(d,i,s)
            if side is None:i+=1;continue
            j=i+1;entry=float(d.iloc[j]["open"]);atr=float(d.iloc[i]["atr"]);risk=STOP_ATR*atr
            if entry<=0 or risk<=0 or risk/entry>MAX_RISK_PCT:i+=1;continue
            stop=entry-risk if side=="LONG" else entry+risk;target=entry+s.target_r*risk if side=="LONG" else entry-s.target_r*risk
            end=min(n-1,j+hold);exit_px=float(d.iloc[end]["close"]);exit_i=end;reason="TIME"
            for k in range(j,end+1):
                hi=float(d.iloc[k]["high"]);lo=float(d.iloc[k]["low"])
                hit_sl=(lo<=stop if side=="LONG" else hi>=stop);hit_tp=(hi>=target if side=="LONG" else lo<=target)
                if hit_sl and hit_tp:exit_px=stop;exit_i=k;reason="AMBIGUOUS_SL";break
                if hit_sl:exit_px=stop;exit_i=k;reason="SL";break
                if hit_tp:exit_px=target;exit_i=k;reason="TP";break
            gross,net=self._pnl(side,entry,exit_px)
            frac=j/max(1,n-1);phase="DEV" if frac<DEV_FRAC else ("VAL" if frac<DEV_FRAC+VAL_FRAC else "TEST")
            out.append({"pair":pair,"direction":side,"entry_time":d.iloc[j]["timestamp"],"exit_time":d.iloc[exit_i]["timestamp"],"gross":gross,"net":net,"phase":phase,"reason":reason})
            i=max(i+1,exit_i+1)
        return out

    @staticmethod
    def _stats(ts:list[dict[str,Any]])->dict[str,Any]:
        vals=[float(t["net"]) for t in ts];gross=[float(t["gross"]) for t in ts];wins=[v for v in vals if v>0];loss=[v for v in vals if v<0];gp=sum(wins);gl=abs(sum(loss));pairs={}
        for t in ts:pairs[t["pair"]]=pairs.get(t["pair"],0.0)+float(t["net"])
        pos_pairs=sum(v>0 for v in pairs.values());positive=sum(v for v in pairs.values() if v>0);top=max([v for v in pairs.values() if v>0],default=0.0)
        return {"n":len(ts),"wr":100*len(wins)/len(ts) if ts else 0.0,"pf":gp/gl if gl else (999.0 if gp else 0.0),"net":sum(vals),"gross":sum(gross),"exp":statistics.mean(vals) if vals else 0.0,"positive_pair_frac":pos_pairs/len(pairs) if pairs else 0.0,"top_pair_share":top/positive if positive>0 else 1.0,"pair_pnl":pairs}

    @staticmethod
    def _bootstrap(vals:list[float],seed:int=260809,nboot:int=2000)->tuple[float,float]:
        if len(vals)<2:return (0.0,0.0)
        rng=random.Random(seed);means=[];n=len(vals)
        for _ in range(nboot):means.append(sum(vals[rng.randrange(n)] for _ in range(n))/n)
        means.sort();return means[int(.025*nboot)],means[min(nboot-1,int(.975*nboot))]

    @staticmethod
    def _portfolio(ts:list[dict[str,Any]],max_open:int=4)->tuple[list[dict[str,Any]],int]:
        accepted=[];active=[];rej=0
        for t in sorted(ts,key=lambda x:(x["entry_time"],x["pair"])):
            active=[a for a in active if a["exit_time"]>t["entry_time"]]
            if len(active)>=max_open:rej+=1;continue
            accepted.append(t);active.append(t)
        return accepted,rej

    @staticmethod
    def _windows(ts:list[dict[str,Any]],parts:int=3)->list[float]:
        z=sorted(ts,key=lambda x:x["entry_time"]);out=[]
        for p in range(parts):
            a=round(len(z)*p/parts);b=round(len(z)*(p+1)/parts);out.append(sum(float(t["net"]) for t in z[a:b]))
        return out

    def run(self)->dict[str,Any]:
        if self.already_done():return {"skipped":True,"version":AUDIT_VERSION}
        pairs=self.pairs[:20];base={};coverage=[]
        for pair in pairs:
            d=self._frame(pair)
            if not d.empty:
                days=max(0.0,(d.iloc[-1]["timestamp"]-d.iloc[0]["timestamp"]).total_seconds()/86400);coverage.append({"pair":pair,"bars":len(d),"days":days})
                base[pair]=d
        specs=self._specs();rows=[];trade_cache={}
        for s in specs:
            all_t=[]
            for pair,d0 in base.items():
                d=self._resample(d0,s.timeframe)
                if len(d)>=240:all_t.extend(self._trades(d,pair,s))
            trade_cache[s.key]=all_t
            dev=[t for t in all_t if t["phase"]=="DEV"];val=[t for t in all_t if t["phase"]=="VAL"]
            ds=self._stats(dev);vs=self._stats(val)
            rows.append({"key":s.key,"spec":s,"dev":ds,"val":vs})
        qualified=[r for r in rows if r["dev"]["n"]>=80 and r["dev"]["pf"]>1.05 and r["dev"]["exp"]>0 and r["dev"]["positive_pair_frac"]>=.40 and r["dev"]["top_pair_share"]<.50]
        qualified.sort(key=lambda r:(min(r["dev"]["pf"],2.0)*math.sqrt(r["dev"]["n"])*(0.5+r["dev"]["positive_pair_frac"])),reverse=True)
        top_dev=qualified[:5]
        val_pass=[r for r in top_dev if r["val"]["n"]>=30 and r["val"]["pf"]>1.0 and r["val"]["exp"]>0 and r["val"]["positive_pair_frac"]>=.35]
        val_pass.sort(key=lambda r:(min(r["dev"]["pf"],r["val"]["pf"])*math.sqrt(min(r["dev"]["n"],r["val"]["n"]))),reverse=True)
        champion=val_pass[0] if val_pass else None
        result={"version":AUDIT_VERSION,"pairs":pairs,"candidate_count":len(specs),"qualified_dev":len(qualified),"val_pass":len(val_pass),"coverage":coverage,"top_dev":[{"key":r["key"],"dev":r["dev"],"val":r["val"]} for r in top_dev],"costs":{"buy":self.buy_fee,"sell":self.sell_fee,"spread":self.spread},"notional":self.notional}
        if champion is None:
            result.update({"champion":None,"verdict":"NO_VALIDATED_EDGE"});self._save_marker(result);return result
        all_t=trade_cache[champion["key"]];test=[t for t in all_t if t["phase"]=="TEST"];ts=self._stats(test);ci=self._bootstrap([float(t["net"]) for t in test]);wins=self._windows(test,3);port,rej=self._portfolio(test,4);ps=self._stats(port)
        positive_windows=sum(v>0 for v in wins)
        promoted=ts["n"]>=40 and ts["pf"]>1.10 and ts["exp"]>0 and ci[0]>0 and positive_windows>=2 and ts["top_pair_share"]<.50
        result.update({"champion":champion["key"],"dev":champion["dev"],"val":champion["val"],"test":ts,"test_bootstrap95":ci,"test_windows":wins,"positive_test_windows":positive_windows,"portfolio_test":ps,"portfolio_rejected":rej,"verdict":"EDGE_CONFIRMED" if promoted else "TEST_FAILED"})
        self._save_marker(result);return result

    @staticmethod
    def format_report(s:dict[str,Any])->str:
        if s.get("skipped"):return ""
        cov=s.get("coverage",[]);days=[float(x.get("days",0)) for x in cov];med=statistics.median(days) if days else 0.0;mn=min(days) if days else 0.0;mx=max(days) if days else 0.0
        lines=["🧠 <b>EDGE DISCOVERY V2 · TEST FALSIFICABILE</b>",f"Crypto con dati: <b>{len(cov)}</b> · sorgente 15m · TF testati: <b>15m/30m/1h/2h/4h</b>",f"Copertura storico per coppia: min <b>{mn:.1f}g</b> · mediana <b>{med:.1f}g</b> · max <b>{mx:.1f}g</b>",f"Candidati predefiniti: <b>{s.get('candidate_count',0)}</b> · DEV qualificati: <b>{s.get('qualified_dev',0)}</b> · passati VALIDATION: <b>{s.get('val_pass',0)}</b>","Costi inclusi in ogni fase: fee 0.160% + 0.160% · spread 0.050%","","🏗 <b>Top DEV (prima di aprire TEST)</b>"]
        top=s.get("top_dev",[])
        if not top:lines.append("• Nessun candidato supera i criteri DEV.")
        for x in top:
            d=x["dev"];v=x["val"];lines.append(f"• {x['key']} · DEV n{d['n']} PF{d['pf']:.2f} Exp€{d['exp']:+.3f} · VAL n{v['n']} PF{v['pf']:.2f} Exp€{v['exp']:+.3f}")
        if not s.get("champion"):
            lines += ["","❌ <b>NESSUN EDGE VALIDATO</b>","La ricerca si ferma prima del TEST finale: nessun candidato ha superato DEV + VALIDATION senza concentrazione eccessiva.","Il live non è stato modificato."]
            return "\n".join(lines)
        d=s["dev"];v=s["val"];t=s["test"];ci=s.get("test_bootstrap95",(0,0));w=s.get("test_windows",[]);p=s.get("portfolio_test",{})
        verdict=s.get("verdict")
        lines += ["",f"🏆 <b>Champion congelato:</b> {s['champion']}",f"DEV: n={d['n']} · PF {d['pf']:.2f} · P&L €{d['net']:+.2f} · Exp €{d['exp']:+.3f}",f"VALIDATION: n={v['n']} · PF {v['pf']:.2f} · P&L €{v['net']:+.2f} · Exp €{v['exp']:+.3f}","","🔒 <b>TEST finale mai usato nella selezione</b>",f"n={t['n']} · WR {t['wr']:.1f}% · PF {t['pf']:.2f} · lordo €{t['gross']:+.2f} · netto €{t['net']:+.2f} · Exp €{t['exp']:+.3f}",f"Bootstrap 95% expectancy: €{ci[0]:+.3f} → €{ci[1]:+.3f}",f"Finestre TEST positive: {s.get('positive_test_windows',0)}/3 · P&L finestre: "+" / ".join(f"€{x:+.2f}" for x in w),f"Concentrazione maggiore coppia sui profitti positivi: {100*t['top_pair_share']:.1f}%",f"Portafoglio €100 max 4×€25: n={p.get('n',0)} · scartati {s.get('portfolio_rejected',0)} · PF {p.get('pf',0):.2f} · P&L €{p.get('net',0):+.2f}",""]
        if verdict=="EDGE_CONFIRMED":lines += ["✅ <b>EDGE CONFERMATO NEL TEST</b>","Candidato idoneo al prossimo passo: paper trading separato, senza sostituire automaticamente il live attuale."]
        else:lines += ["❌ <b>TEST FINALE FALLITO</b>","Il champion non soddisfa i criteri predefiniti fuori campione. Non verranno ritoccati i parametri usando questo TEST."]
        lines.append("Nessuna modifica al bot live.")
        return "\n".join(lines)
