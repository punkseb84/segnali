"""One-shot concentration check: frozen cost-aware volatility regime excluding CC/USD.
No parameters are changed; diagnostic only.
"""
from __future__ import annotations
from collections import defaultdict
from typing import Any
import json, random, statistics
from project.cost_aware_volatility_walkforward_audit import CostAwareVolatilityWalkForwardAudit, VOL_MIN, VOL_MAX

AUDIT_VERSION='COST_AWARE_VOLATILITY_EX_CC_V1_20260809'
WINDOWS=5
BOOTSTRAP_N=3000

class CostAwareVolatilityExCCAudit(CostAwareVolatilityWalkForwardAudit):
    def already_done(self)->bool:
        self._ensure_marker();return bool(self.postgres.fetch_all('SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1',(AUDIT_VERSION,)))
    def _save_marker(self,s:dict[str,Any])->None:
        self.postgres.execute('INSERT INTO statistics.velez_candidate_audit_runs(version,summary) VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING',(AUDIT_VERSION,json.dumps(s,default=str)))
    @staticmethod
    def _bootstrap_ci(vals:list[float],n:int=BOOTSTRAP_N,seed:int=23)->tuple[float,float]:
        if len(vals)<2:return (0.0,0.0)
        rng=random.Random(seed);L=len(vals);means=[]
        for _ in range(n):means.append(statistics.mean(vals[rng.randrange(L)] for _ in range(L)))
        means.sort();return means[int(.025*n)],means[min(n-1,int(.975*n))]
    def run(self):
        if self.already_done():return {'skipped':True}
        base,fixed,cs,ce=self._exact_fixed_sample();aug=self._augment(base,fixed)
        frozen=[r for r in aug if VOL_MIN<=r['vol1h']<=VOL_MAX and r.get('t1.0_pnl') is not None]
        rows=sorted([r for r in frozen if r['pair']!='CC/USD'],key=lambda r:r['time'])
        windows=[]
        for i in range(WINDOWS):
            a=int(len(rows)*i/WINDOWS);b=int(len(rows)*(i+1)/WINDOWS);part=rows[a:b];st=self._stats(part)
            windows.append({'id':i+1,'n':st['n'],'wr':st['wr'],'pf':st['pf'],'net':st['net'],'exp':st['exp']})
        overall=self._stats(rows);vals=[float(r['t1.0_pnl']) for r in rows];lo,hi=self._bootstrap_ci(vals)
        bypair=defaultdict(list)
        for r in rows:bypair[r['pair']].append(float(r['t1.0_pnl']))
        pair_pnl={p:sum(v) for p,v in bypair.items()};positive_total=sum(v for v in pair_pnl.values() if v>0)
        top_pair=max(pair_pnl,key=lambda p:pair_pnl[p]) if pair_pnl else ''
        top_share=pair_pnl[top_pair]/positive_total if top_pair and positive_total>0 and pair_pnl[top_pair]>0 else 0.0
        pos=sum(w['pf']>1 and w['exp']>0 for w in windows)
        # Same qualitative robustness question, but no promotion decision because sample shrinks by design.
        s={'version':AUDIT_VERSION,'original_rows':len(base),'frozen_rows':len(frozen),'rows':len(rows),'excluded_cc':len(frozen)-len(rows),'fixed_pairs':fixed,'common_start':str(cs),'common_end':str(ce),'vol_min':VOL_MIN,'vol_max':VOL_MAX,'windows':windows,'positive_windows':pos,'overall':overall,'boot_lo':lo,'boot_hi':hi,'pair_pnl':pair_pnl,'top_pair':top_pair,'top_pair_share_positive':top_share,'robust_ex_cc':pos>=3 and overall['pf']>1 and overall['exp']>0 and lo>0}
        self._save_marker(s);return s
    @staticmethod
    def format_report(s):
        if s.get('skipped'):return ''
        o=s.get('overall',{});lines=['🧪 <b>AUDIT COST-AWARE · EX CC/USD</b>',f"Regole congelate: <b>costi ≤20%R · TP 1R · vol1h {s.get('vol_min',0):.3f}-{s.get('vol_max',0):.3f}%</b>",f"Trade filtro originale: {s.get('frozen_rows',0)} · esclusi CC/USD: {s.get('excluded_cc',0)} · restanti: <b>{s.get('rows',0)}</b>",'','📆 <b>5 finestre cronologiche senza CC/USD</b>']
        for w in s.get('windows',[]):lines.append(f"• W{w['id']}: n={w['n']} · WR {w['wr']:.1f}% · PF {w['pf']:.2f} · P&L €{w['net']:+.2f} · Exp €{w['exp']:+.3f}")
        lines += ['',f"Finestre positive: <b>{s.get('positive_windows',0)}/5</b>",f"Aggregato ex-CC: n={o.get('n',0)} · WR {o.get('wr',0):.1f}% · PF <b>{o.get('pf',0):.2f}</b> · P&L <b>€{o.get('net',0):+.2f}</b> · Exp <b>€{o.get('exp',0):+.3f}</b>",f"Bootstrap 95% expectancy: <b>€{s.get('boot_lo',0):+.3f} → €{s.get('boot_hi',0):+.3f}</b>",f"Coppia più contributiva rimasta: <b>{s.get('top_pair','-')}</b> · quota profitti positivi <b>{100*s.get('top_pair_share_positive',0):.1f}%</b>",'','📦 <b>P&L per coppia ex-CC</b>']
        for p,v in sorted(s.get('pair_pnl',{}).items(),key=lambda x:x[1],reverse=True):lines.append(f"• {p}: €{v:+.2f}")
        lines += ['',('✅ <b>EDGE SOPRAVVIVE SENZA CC/USD</b>' if s.get('robust_ex_cc') else '❌ <b>EDGE NON CONFERMATO SENZA CC/USD</b>')]
        lines.append('⚠️ Nessun parametro modificato rispetto al test precedente. Questo audit serve solo a misurare la dipendenza da CC/USD; nessuna modifica al live.')
        return '\n'.join(lines)
