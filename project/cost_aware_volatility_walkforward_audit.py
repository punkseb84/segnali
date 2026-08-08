"""Final one-shot validation of the frozen cost-aware volatility regime.

Rules are frozen before this test:
- cost_r <= 0.20
- TP = 1R
- vol1h between 0.515% and 0.767%
Uses the exact fixed-universe sample construction and no live changes.
"""
from __future__ import annotations
from collections import defaultdict
from typing import Any
import json, random, statistics
from project.cost_aware_regime_validation_audit import CostAwareRegimeValidationAudit

AUDIT_VERSION='COST_AWARE_VOLATILITY_WALKFORWARD_V1_20260808'
VOL_MIN=0.515
VOL_MAX=0.767
WINDOWS=5
BOOTSTRAP_N=3000

class CostAwareVolatilityWalkForwardAudit(CostAwareRegimeValidationAudit):
    def already_done(self)->bool:
        self._ensure_marker();return bool(self.postgres.fetch_all('SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1',(AUDIT_VERSION,)))
    def _save_marker(self,s:dict[str,Any])->None:
        self.postgres.execute('INSERT INTO statistics.velez_candidate_audit_runs(version,summary) VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING',(AUDIT_VERSION,json.dumps(s,default=str)))
    @staticmethod
    def _bootstrap_ci(vals:list[float],n:int=BOOTSTRAP_N,seed:int=17)->tuple[float,float]:
        if len(vals)<2:return (0.0,0.0)
        rng=random.Random(seed);L=len(vals);means=[]
        for _ in range(n):means.append(statistics.mean(vals[rng.randrange(L)] for _ in range(L)))
        means.sort();return means[int(.025*n)],means[min(n-1,int(.975*n))]
    def run(self):
        if self.already_done():return {'skipped':True}
        base,fixed,cs,ce=self._exact_fixed_sample();aug=self._augment(base,fixed)
        rows=[r for r in aug if VOL_MIN<=r['vol1h']<=VOL_MAX and r.get('t1.0_pnl') is not None]
        rows=sorted(rows,key=lambda r:r['time'])
        windows=[]
        for i in range(WINDOWS):
            a=int(len(rows)*i/WINDOWS);b=int(len(rows)*(i+1)/WINDOWS);part=rows[a:b];st=self._stats(part)
            windows.append({'id':i+1,'n':st['n'],'wr':st['wr'],'pf':st['pf'],'net':st['net'],'exp':st['exp'],'from':str(part[0]['time']) if part else '', 'to':str(part[-1]['time']) if part else ''})
        overall=self._stats(rows);vals=[float(r['t1.0_pnl']) for r in rows];lo,hi=self._bootstrap_ci(vals)
        bypair=defaultdict(list)
        for r in rows:bypair[r['pair']].append(float(r['t1.0_pnl']))
        pair_pnl={p:sum(v) for p,v in bypair.items()};positive_total=sum(v for v in pair_pnl.values() if v>0)
        top_pair=max(pair_pnl,key=lambda p:pair_pnl[p]) if pair_pnl else ''
        top_share=pair_pnl[top_pair]/positive_total if top_pair and positive_total>0 and pair_pnl[top_pair]>0 else 0.0
        positive_windows=sum(w['pf']>1 and w['exp']>0 for w in windows)
        # Predeclared promotion gate; not optimized from results.
        promoted=positive_windows>=4 and overall['pf']>1 and overall['exp']>0 and lo>0 and top_share<0.50 and overall['n']>=40
        s={'version':AUDIT_VERSION,'original_rows':len(base),'augmented_rows':len(aug),'rows':len(rows),'fixed_pairs':fixed,'common_start':str(cs),'common_end':str(ce),'vol_min':VOL_MIN,'vol_max':VOL_MAX,'windows':windows,'positive_windows':positive_windows,'overall':overall,'boot_lo':lo,'boot_hi':hi,'pair_pnl':pair_pnl,'top_pair':top_pair,'top_pair_share_positive':top_share,'promoted':promoted}
        self._save_marker(s);return s
    @staticmethod
    def format_report(s):
        if s.get('skipped'):return ''
        o=s.get('overall',{});lines=['🧊 <b>AUDIT COST-AWARE · VOLATILITÀ CONGELATA</b>',f"Regole: <b>costi ≤20%R · TP 1R · vol1h {s.get('vol_min',0):.3f}-{s.get('vol_max',0):.3f}%</b>",f"Campione originale: {s.get('original_rows',0)} · con regime: {s.get('augmented_rows',0)} · filtrati: <b>{s.get('rows',0)}</b>",'','📆 <b>5 finestre cronologiche</b>']
        for w in s.get('windows',[]):lines.append(f"• W{w['id']}: n={w['n']} · WR {w['wr']:.1f}% · PF {w['pf']:.2f} · P&L €{w['net']:+.2f} · Exp €{w['exp']:+.3f}")
        lines += ['',f"Finestre positive: <b>{s.get('positive_windows',0)}/5</b>",f"Aggregato: n={o.get('n',0)} · WR {o.get('wr',0):.1f}% · PF <b>{o.get('pf',0):.2f}</b> · P&L <b>€{o.get('net',0):+.2f}</b> · Exp <b>€{o.get('exp',0):+.3f}</b>",f"Bootstrap 95% expectancy: <b>€{s.get('boot_lo',0):+.3f} → €{s.get('boot_hi',0):+.3f}</b>",f"Coppia più contributiva: <b>{s.get('top_pair','-')}</b> · quota profitti positivi <b>{100*s.get('top_pair_share_positive',0):.1f}%</b>",'','📦 <b>P&L per coppia</b>']
        for p,v in sorted(s.get('pair_pnl',{}).items(),key=lambda x:x[1],reverse=True):lines.append(f"• {p}: €{v:+.2f}")
        lines += ['',('✅ <b>PROMOTION GATE SUPERATO</b>: candidato idoneo a paper trading dedicato.' if s.get('promoted') else '❌ <b>PROMOTION GATE NON SUPERATO</b>: il filtro non è ancora abbastanza stabile.')]
        lines.append('⚠️ Gate predefinito: almeno 4/5 finestre positive, PF aggregato >1, expectancy >0, bootstrap 95% interamente >0, almeno 40 trade e nessuna coppia oltre il 50% dei profitti positivi. Nessuna modifica al live.')
        return '\n'.join(lines)
