"""Corrected regime validation for the exact fixed-universe cost-aware sample.
Reuses the same 78-trade construction as CostAwareWalkForwardAudit, loads BTC/USD
explicitly, derives thresholds only from the first 60% and validates on the last 40%.
Diagnostic only; no live changes.
"""
from __future__ import annotations
from typing import Any
import json, statistics
import pandas as pd
from project.cost_aware_walkforward_audit import CostAwareWalkForwardAudit

AUDIT_VERSION='COST_AWARE_REGIME_VALIDATION_FIXED_SAMPLE_V2_20260808'

class CostAwareRegimeValidationAudit(CostAwareWalkForwardAudit):
    def already_done(self)->bool:
        self._ensure_marker();return bool(self.postgres.fetch_all('SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1',(AUDIT_VERSION,)))
    def _save_marker(self,s:dict[str,Any])->None:
        self.postgres.execute('INSERT INTO statistics.velez_candidate_audit_runs(version,summary) VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING',(AUDIT_VERSION,json.dumps(s,default=str)))

    def _exact_fixed_sample(self):
        rows=self._rows();cov=self._pair_coverage()
        if not rows or not cov:return [],[],None,None
        common_start=max(v[0] for v in cov.values());common_end=min(v[1] for v in cov.values())
        fixed=sorted([p for p,(a,b,n) in cov.items() if a<=common_start and b>=common_end])
        rr=[r for r in rows if r['pair'] in fixed and r['time']>=common_start and r['time']<=common_end and r['cost_r']<=.20 and r['t1.0_pnl'] is not None]
        return sorted(rr,key=lambda r:r['time']),fixed,common_start,common_end

    def _frame_explicit(self,pair:str):
        # _frame works independently of self.pairs; calling it explicitly prevents
        # BTC from disappearing when dynamic market-cap universe omits it.
        return self._frame(pair)

    @staticmethod
    def _hourly_context(d:pd.DataFrame,t:Any):
        x=d[d['timestamp']<t]['close'].astype(float).to_numpy()
        x=x[(len(x)%4):]
        if len(x)<208:return None
        h=pd.Series(x.reshape(-1,4)[:,-1]);e20=h.ewm(span=20,adjust=False).mean();e50=h.ewm(span=50,adjust=False).mean()
        ret=h.pct_change().dropna().tail(20)
        return {'close':float(h.iloc[-1]),'e20':float(e20.iloc[-1]),'e50':float(e50.iloc[-1]),'slope':10000*(float(e20.iloc[-1])/float(e20.iloc[-2])-1) if e20.iloc[-2] else 0.0,'sep':100*abs(float(e20.iloc[-1])/float(e50.iloc[-1])-1) if e50.iloc[-1] else 0.0,'vol':100*float(ret.std()) if len(ret)>=5 else 0.0}

    def _augment(self,rows,fixed):
        frames={p:self._frame_explicit(p) for p in fixed}
        btc=self._frame_explicit('BTC/USD')
        out=[]
        for r in rows:
            ctx=self._hourly_context(frames.get(r['pair'],pd.DataFrame()),r['time'])
            bctx=self._hourly_context(btc,r['time']) if btc is not None and not btc.empty else None
            if ctx is None or bctx is None:continue
            btc_dir=1 if bctx['close']>bctx['e20']>bctx['e50'] else (-1 if bctx['close']<bctx['e20']<bctx['e50'] else 0)
            bull=tot=0
            for fd in frames.values():
                c=self._hourly_context(fd,r['time'])
                if c is None:continue
                tot+=1;bull+=int(c['close']>c['e20'])
            x=dict(r);x.update({'vol1h':ctx['vol'],'slope20h':ctx['slope'],'sep2050':ctx['sep'],'btc_dir':btc_dir,'btc_slope':bctx['slope'],'breadth':100*bull/tot if tot else 0.0});out.append(x)
        return out

    @staticmethod
    def _stats(rows):return CostAwareWalkForwardAudit._stats(rows,1.0)

    def run(self):
        if self.already_done():return {'skipped':True}
        base,fixed,cs,ce=self._exact_fixed_sample();rows=self._augment(base,fixed)
        cut=int(len(rows)*.60);dev,hold=rows[:cut],rows[cut:]
        if not dev:
            s={'version':AUDIT_VERSION,'rows':len(rows),'fixed_pairs':fixed};self._save_marker(s);return s
        slope_med=statistics.median(abs(r['slope20h']) for r in dev);sep_med=statistics.median(r['sep2050'] for r in dev);breadth_med=statistics.median(r['breadth'] for r in dev);vols=sorted(r['vol1h'] for r in dev);q25=vols[int(.25*(len(vols)-1))];q75=vols[int(.75*(len(vols)-1))]
        def aligned_slope(r):return (r['direction']=='LONG' and r['slope20h']>0) or (r['direction']=='SHORT' and r['slope20h']<0)
        def btc_aligned(r):return (r['direction']=='LONG' and r['btc_dir']==1) or (r['direction']=='SHORT' and r['btc_dir']==-1)
        filters=[
            ('slope aligned',aligned_slope),
            (f'abs slope ≥ {slope_med:.2f}bp',lambda r,m=slope_med:abs(r['slope20h'])>=m),
            (f'sep EMA20/50 ≥ {sep_med:.3f}%',lambda r,m=sep_med:r['sep2050']>=m),
            (f'breadth ≥ {breadth_med:.1f}%',lambda r,m=breadth_med:r['breadth']>=m),
            (f'vol1h {q25:.3f}-{q75:.3f}%',lambda r,a=q25,b=q75:a<=r['vol1h']<=b),
            ('BTC aligned',btc_aligned),
            ('slope aligned + BTC aligned',lambda r:aligned_slope(r) and btc_aligned(r)),
            ('slope aligned + sep strong',lambda r,m=sep_med:aligned_slope(r) and r['sep2050']>=m),
        ]
        tested=[]
        for name,fn in filters:
            d=[r for r in dev if fn(r)];h=[r for r in hold if fn(r)];tested.append({'filter':name,'dev':self._stats(d),'hold':self._stats(h)})
        robust=[x for x in tested if x['dev']['n']>=12 and x['hold']['n']>=8 and x['dev']['pf']>1 and x['hold']['pf']>1 and x['dev']['exp']>0 and x['hold']['exp']>0]
        # Five windows on the exact same augmented sample for BTC-state diagnostics.
        windows=[]
        for i in range(5):
            a=int(len(rows)*i/5);b=int(len(rows)*(i+1)/5);p=rows[a:b];st=self._stats(p)
            windows.append({'id':i+1,'n':len(p),'pf':st['pf'],'exp':st['exp'],'btc_up':sum(r['btc_dir']==1 for r in p),'btc_flat':sum(r['btc_dir']==0 for r in p),'btc_down':sum(r['btc_dir']==-1 for r in p)})
        s={'version':AUDIT_VERSION,'rows':len(rows),'original_fixed_rows':len(base),'fixed_pairs':fixed,'common_start':str(cs),'common_end':str(ce),'dev':self._stats(dev),'hold':self._stats(hold),'thresholds':{'slope_abs_med':slope_med,'sep_med':sep_med,'breadth_med':breadth_med,'vol_q25':q25,'vol_q75':q75},'tested':tested,'robust':robust,'windows':windows}
        self._save_marker(s);return s

    @staticmethod
    def format_report(s):
        if s.get('skipped'):return ''
        d=s.get('dev',{});h=s.get('hold',{});lines=['🧪 <b>AUDIT COST-AWARE · REGIME VALIDATO</b>',f"Campione walk-forward originale: <b>{s.get('original_fixed_rows',0)}</b> · arricchito con regime: <b>{s.get('rows',0)}</b>",f"Universo fisso: <b>{len(s.get('fixed_pairs',[]))}</b> coppie",f"DEV 60%: n={d.get('n',0)} PF {d.get('pf',0):.2f} Exp €{d.get('exp',0):+.3f} · HOLD 40%: n={h.get('n',0)} PF {h.get('pf',0):.2f} Exp €{h.get('exp',0):+.3f}",'','₿ <b>BTC esplicito · 5 finestre</b>']
        for w in s.get('windows',[]):lines.append(f"• W{w['id']}: n={w['n']} PF {w['pf']:.2f} Exp €{w['exp']:+.3f} · BTC UP {w['btc_up']} / FLAT {w['btc_flat']} / DOWN {w['btc_down']}")
        lines += ['','🔎 <b>Filtri definiti sul DEV → verificati HOLD</b>']
        for x in s.get('tested',[]):
            a,b=x['dev'],x['hold'];lines.append(f"• {x['filter']}: DEV n={a['n']} PF {a['pf']:.2f} Exp €{a['exp']:+.3f} · HOLD n={b['n']} PF {b['pf']:.2f} Exp €{b['exp']:+.3f}")
        lines += ['','✅ <b>Filtri robusti DEV + HOLD</b>']
        if not s.get('robust'):lines.append('• Nessuno supera PF 1 ed expectancy positiva in entrambi i periodi con campione minimo.')
        else:
            for x in s['robust']:lines.append(f"• {x['filter']} · DEV PF {x['dev']['pf']:.2f} · HOLD PF {x['hold']['pf']:.2f}")
        lines.append('\n⚠️ Nessun look-ahead: soglie calcolate solo sul primo 60%; BTC/USD è caricato esplicitamente; nessuna modifica al live.')
        return '\n'.join(lines)
