"""One-shot fixed-universe walk-forward robustness audit for cost-aware strategy.
Keeps cost_r <= 0.20 and TP=1R frozen. Diagnostic only; no live changes.
"""
from __future__ import annotations
from typing import Any
from collections import defaultdict
import json, random, statistics
from project.cost_aware_strategy_audit import CostAwareStrategyAudit

AUDIT_VERSION="COST_AWARE_WALKFORWARD_FIXED_UNIVERSE_V1_20260808"

class CostAwareWalkForwardAudit(CostAwareStrategyAudit):
    WINDOWS=5
    BOOTSTRAP_N=2000
    def already_done(self)->bool:
        self._ensure_marker()
        return bool(self.postgres.fetch_all("SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1",(AUDIT_VERSION,)))
    def _save_marker(self,s:dict[str,Any])->None:
        self.postgres.execute("INSERT INTO statistics.velez_candidate_audit_runs(version,summary) VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING",(AUDIT_VERSION,json.dumps(s,default=str)))
    def _pair_coverage(self)->dict[str,tuple[Any,Any,int]]:
        cov={}
        for p in self.pairs:
            d=self._frame(p)
            if len(d)>=260: cov[p]=(d.iloc[0]['timestamp'],d.iloc[-1]['timestamp'],len(d))
        return cov
    @staticmethod
    def _bootstrap_ci(vals:list[float],n:int=2000,seed:int=7)->tuple[float,float]:
        if len(vals)<2:return (0.0,0.0)
        rng=random.Random(seed); means=[]; L=len(vals)
        for _ in range(n):means.append(statistics.mean(vals[rng.randrange(L)] for _ in range(L)))
        means.sort();return means[int(.025*n)],means[min(n-1,int(.975*n))]
    def run(self)->dict[str,Any]:
        if self.already_done():return {'skipped':True}
        rows=self._rows();cov=self._pair_coverage()
        if not rows:return {'version':AUDIT_VERSION,'rows':0}
        # Fixed universe = pairs whose stored history spans the full common interval across all selected pairs.
        starts=[v[0] for v in cov.values()];ends=[v[1] for v in cov.values()]
        common_start=max(starts) if starts else None;common_end=min(ends) if ends else None
        fixed=sorted([p for p,(a,b,n) in cov.items() if common_start is not None and a<=common_start and b>=common_end])
        rr=[r for r in rows if r['pair'] in fixed and r['time']>=common_start and r['time']<=common_end and r['cost_r']<=.20 and r['t1.0_pnl'] is not None]
        rr=sorted(rr,key=lambda r:r['time'])
        windows=[]
        for i in range(self.WINDOWS):
            a=int(len(rr)*i/self.WINDOWS);b=int(len(rr)*(i+1)/self.WINDOWS);part=rr[a:b]
            st=self._stats(part,1.0);st['from']=str(part[0]['time']) if part else '';st['to']=str(part[-1]['time']) if part else '';windows.append(st)
        overall=self._stats(rr,1.0);vals=[float(r['t1.0_pnl']) for r in rr];lo,hi=self._bootstrap_ci(vals,self.BOOTSTRAP_N)
        bypair=defaultdict(list)
        for r in rr:bypair[r['pair']].append(float(r['t1.0_pnl']))
        pair_pnl={p:sum(v) for p,v in bypair.items()};positive_total=sum(v for v in pair_pnl.values() if v>0)
        top_pair=max(pair_pnl,key=lambda p:pair_pnl[p]) if pair_pnl else ''
        concentration=(pair_pnl[top_pair]/positive_total if top_pair and positive_total>0 else 0.0)
        s={'version':AUDIT_VERSION,'rows':len(rr),'fixed_pairs':fixed,'common_start':str(common_start),'common_end':str(common_end),'windows':windows,'overall':overall,'boot_lo':lo,'boot_hi':hi,'positive_windows':sum(w['pf']>1 and w['exp']>0 for w in windows),'pair_pnl':pair_pnl,'top_pair':top_pair,'top_pair_share_positive':concentration}
        self._save_marker(s);return s
    @staticmethod
    def format_report(s:dict[str,Any])->str:
        if s.get('skipped'):return ''
        lines=["🧭 <b>AUDIT COST-AWARE · WALK-FORWARD</b>","Regole congelate: <b>costi ≤20% di 1R · TP 1R</b>",f"Universo fisso: <b>{len(s.get('fixed_pairs',[]))}</b> coppie · trade <b>{s.get('rows',0)}</b>",f"Intervallo comune: {s.get('common_start','')} → {s.get('common_end','')}","","📆 <b>5 finestre cronologiche</b>"]
        for i,w in enumerate(s.get('windows',[]),1):lines.append(f"• W{i}: n={w['n']} · WR {w['wr']:.1f}% · PF {w['pf']:.2f} · P&L €{w['net']:+.2f} · Exp €{w['exp']:+.3f}")
        o=s.get('overall',{});lines += ["",f"Finestre positive: <b>{s.get('positive_windows',0)}/5</b>",f"Aggregato: n={o.get('n',0)} · WR {o.get('wr',0):.1f}% · PF <b>{o.get('pf',0):.2f}</b> · P&L <b>€{o.get('net',0):+.2f}</b> · Exp <b>€{o.get('exp',0):+.3f}</b>",f"Bootstrap 95% expectancy: <b>€{s.get('boot_lo',0):+.3f} → €{s.get('boot_hi',0):+.3f}</b>",f"Coppia più contributiva: <b>{s.get('top_pair','-')}</b> · quota dei profitti positivi <b>{100*s.get('top_pair_share_positive',0):.1f}%</b>","","📦 <b>P&L per coppia</b>"]
        for p,v in sorted(s.get('pair_pnl',{}).items(),key=lambda x:x[1],reverse=True):lines.append(f"• {p}: €{v:+.2f}")
        lines.append("\n⚠️ One-shot diagnostico. Universo, cost cap e target sono congelati prima del test; nessuna modifica al live.")
        return '\n'.join(lines)
