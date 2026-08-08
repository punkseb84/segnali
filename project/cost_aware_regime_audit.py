"""One-shot regime diagnostics for the fixed cost-aware slice.
Uses only information available before each entry. No live rule changes.
"""
from __future__ import annotations
from typing import Any
import json, statistics
import pandas as pd
from project.cost_aware_strategy_audit import CostAwareStrategyAudit

AUDIT_VERSION='COST_AWARE_REGIME_AUDIT_V1_20260808'

class CostAwareRegimeAudit(CostAwareStrategyAudit):
    def already_done(self)->bool:
        self._ensure_marker();return bool(self.postgres.fetch_all('SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1',(AUDIT_VERSION,)))
    def _save_marker(self,s:dict[str,Any])->None:
        self.postgres.execute('INSERT INTO statistics.velez_candidate_audit_runs(version,summary) VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING',(AUDIT_VERSION,json.dumps(s,default=str)))
    @staticmethod
    def _med(rows,key):
        vals=[float(r[key]) for r in rows if r.get(key) is not None]
        return statistics.median(vals) if vals else 0.0
    def _augment(self,rows):
        frames={p:self._frame(p) for p in self.pairs}
        btc=frames.get('BTC/USD')
        out=[]
        for r in rows:
            d=frames.get(r['pair'])
            if d is None or d.empty:continue
            idx=d.index[d['timestamp']<r['time']]
            if len(idx)<220:continue
            i=int(idx[-1]); prior=d.iloc[:i+1]
            c=prior['close'].astype(float)
            # 1h bars from completed 15m groups.
            arr=c.to_numpy(); arr=arr[(len(arr)%4):]
            if len(arr)<208:continue
            hourly=arr.reshape(-1,4)[:,-1]
            hs=pd.Series(hourly)
            e20=hs.ewm(span=20,adjust=False).mean();e50=hs.ewm(span=50,adjust=False).mean()
            slope20=10000*(e20.iloc[-1]/e20.iloc[-2]-1) if e20.iloc[-2] else 0.0
            sep=100*abs(e20.iloc[-1]/e50.iloc[-1]-1) if e50.iloc[-1] else 0.0
            ret=hs.pct_change().dropna().tail(20); vol=100*float(ret.std()) if len(ret)>=5 else 0.0
            # BTC direction from prior 1h context.
            btc_dir=0
            if btc is not None and not btc.empty:
                b=btc[btc['timestamp']<r['time']]['close'].astype(float).to_numpy();b=b[(len(b)%4):]
                if len(b)>=208:
                    bh=pd.Series(b.reshape(-1,4)[:,-1]);be20=bh.ewm(span=20,adjust=False).mean();be50=bh.ewm(span=50,adjust=False).mean();btc_dir=1 if bh.iloc[-1]>be20.iloc[-1]>be50.iloc[-1] else (-1 if bh.iloc[-1]<be20.iloc[-1]<be50.iloc[-1] else 0)
            # Breadth: share of available pairs whose last completed 1h close is above EMA20.
            bull=0;tot=0
            for p,fd in frames.items():
                if fd is None or fd.empty:continue
                a=fd[fd['timestamp']<r['time']]['close'].astype(float).to_numpy();a=a[(len(a)%4):]
                if len(a)<84:continue
                hh=pd.Series(a.reshape(-1,4)[:,-1]);ee=hh.ewm(span=20,adjust=False).mean();tot+=1;bull+=int(hh.iloc[-1]>ee.iloc[-1])
            breadth=100*bull/tot if tot else 0.0
            x=dict(r);x.update({'vol1h':vol,'slope20h':slope20,'sep2050':sep,'btc_dir':btc_dir,'breadth':breadth});out.append(x)
        return out
    @staticmethod
    def _stats(rows):return CostAwareStrategyAudit._stats(rows,1.0)
    def run(self):
        if self.already_done():return {'skipped':True}
        base=[r for r in self._rows() if r['cost_r']<=.20 and r.get('t1.0_outcome') in {'TP','SL'}]
        rows=self._augment(base)
        # reproduce five chronological windows from fixed-universe audit style
        n=len(rows); windows=[]
        for k in range(5):
            a=round(k*n/5);b=round((k+1)*n/5);chunk=rows[a:b];windows.append({'id':k+1,'n':len(chunk),'stats':self._stats(chunk),'med':{m:self._med(chunk,m) for m in ('vol1h','slope20h','sep2050','breadth')}})
        good=[r for w in (windows[2:4]) for r in rows[round((w['id']-1)*n/5):round(w['id']*n/5)]] if n else []
        bad=[r for i in (0,1,4) for r in rows[round(i*n/5):round((i+1)*n/5)]] if n else []
        summary={'version':AUDIT_VERSION,'rows':len(rows),'windows':windows,'good_med':{m:self._med(good,m) for m in ('vol1h','slope20h','sep2050','breadth')},'bad_med':{m:self._med(bad,m) for m in ('vol1h','slope20h','sep2050','breadth')},'btc_good':{str(v):sum(r['btc_dir']==v for r in good) for v in (-1,0,1)},'btc_bad':{str(v):sum(r['btc_dir']==v for r in bad) for v in (-1,0,1)}}
        self._save_marker(summary);return summary
    @staticmethod
    def format_report(s):
        if s.get('skipped'):return ''
        lines=['🌦 <b>AUDIT COST-AWARE · REGIMI</b>',f"Trade analizzati: <b>{s.get('rows',0)}</b> · regole congelate costi ≤20%R · TP 1R",'', '<b>Finestre cronologiche</b>']
        for w in s.get('windows',[]):
            st=w['stats'];m=w['med'];lines.append(f"• W{w['id']}: n={w['n']} PF {st['pf']:.2f} Exp €{st['exp']:+.3f} · vol1h {m['vol1h']:.3f}% · slope20 {m['slope20h']:.2f}bp · sep20/50 {m['sep2050']:.3f}% · breadth {m['breadth']:.1f}%")
        g=s.get('good_med',{});b=s.get('bad_med',{});lines += ['', '<b>Mediane W3-W4 vs W1-W2-W5</b>',f"• volatilità 1h: {g.get('vol1h',0):.3f}% vs {b.get('vol1h',0):.3f}%",f"• pendenza EMA20 1h: {g.get('slope20h',0):.2f}bp vs {b.get('slope20h',0):.2f}bp",f"• separazione EMA20/50: {g.get('sep2050',0):.3f}% vs {b.get('sep2050',0):.3f}%",f"• breadth rialzista: {g.get('breadth',0):.1f}% vs {b.get('breadth',0):.1f}%",f"• BTC regime W3-W4: {s.get('btc_good',{})}",f"• BTC regime altre finestre: {s.get('btc_bad',{})}",'','⚠️ Diagnostico descrittivo: nessuna soglia viene applicata al live e nessuna selezione viene fatta usando dati futuri.']
        return '\n'.join(lines)
