"""One-shot robustness audit for the promising cost-aware <=20%R, TP1R slice."""
from __future__ import annotations
from collections import defaultdict
from typing import Any
import json
from project.cost_aware_strategy_audit import CostAwareStrategyAudit

AUDIT_VERSION="COST_AWARE_EDGE_BREAKDOWN_V1_20260808"
RESEND_VERSION="COST_AWARE_EDGE_BREAKDOWN_V1_TELEGRAM_RESEND_20260808"

class CostAwareEdgeBreakdownAudit(CostAwareStrategyAudit):
    def _cached(self):
        self._ensure_marker()
        rows=self.postgres.fetch_all("SELECT summary FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1",(AUDIT_VERSION,))
        if not rows:return None
        row=rows[0]
        return row[0] if isinstance(row,(list,tuple)) else row.get('summary')
    def already_done(self)->bool:
        self._ensure_marker()
        return bool(self.postgres.fetch_all("SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1",(AUDIT_VERSION,)))
    def _save_marker(self,s:dict[str,Any])->None:
        self.postgres.execute("INSERT INTO statistics.velez_candidate_audit_runs(version,summary) VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING",(AUDIT_VERSION,json.dumps(s,default=str)))
    @staticmethod
    def _bucket_stats(rows): return CostAwareStrategyAudit._stats(rows,1.0)
    def run(self):
        cached=self._cached()
        resend_done=bool(self.postgres.fetch_all("SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1",(RESEND_VERSION,)))
        if cached is not None and not resend_done:
            self.postgres.execute("INSERT INTO statistics.velez_candidate_audit_runs(version,summary) VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING",(RESEND_VERSION,json.dumps({'cached_resend':True})))
            if isinstance(cached,str):cached=json.loads(cached)
            cached['cached_resend']=True
            return cached
        if cached is not None:return {"skipped":True}
        rows=self._rows(); cut=int(len(rows)*.70); dev0,hold0=rows[:cut],rows[cut:]
        dev=[r for r in dev0 if r['cost_r']<=.20]; hold=[r for r in hold0 if r['cost_r']<=.20]
        def breakdown(rs,keyfn):
            g=defaultdict(list)
            for r in rs:g[str(keyfn(r))].append(r)
            return {k:self._bucket_stats(v) for k,v in sorted(g.items())}
        def risk_bucket(r):
            x=r['risk_pct']
            return 'sotto 2%' if x<2 else ('2-3%' if x<3 else '3% o più')
        def hour_bucket(r):
            h=r['hour']; return '00-06' if h<6 else ('06-12' if h<12 else ('12-18' if h<18 else '18-24'))
        s={"version":AUDIT_VERSION,"rows":len(rows),"dev":self._bucket_stats(dev),"hold":self._bucket_stats(hold),
           "direction":{"dev":breakdown(dev,lambda r:r['direction']),"hold":breakdown(hold,lambda r:r['direction'])},
           "hour":{"dev":breakdown(dev,hour_bucket),"hold":breakdown(hold,hour_bucket)},
           "risk":{"dev":breakdown(dev,risk_bucket),"hold":breakdown(hold,risk_bucket)},
           "pair":{"dev":breakdown(dev,lambda r:r['pair']),"hold":breakdown(hold,lambda r:r['pair'])}}
        self._save_marker(s);return s
    @staticmethod
    def format_report(s):
        if s.get('skipped'):return ''
        d,h=s['dev'],s['hold']; lines=["🔬 <b>AUDIT COST-AWARE · ROBUSTEZZA EDGE</b>","Slice congelata: <b>costi ≤20% di 1R · TP 1R</b>",f"DEV n={d['n']} · WR {d['wr']:.1f}% · PF {d['pf']:.2f} · P&L €{d['net']:+.2f} · Exp €{d['exp']:+.3f}",f"HOLD n={h['n']} · WR {h['wr']:.1f}% · PF {h['pf']:.2f} · P&L €{h['net']:+.2f} · Exp €{h['exp']:+.3f}"]
        labels=[('direction','LONG / SHORT'),('hour','Fasce UTC'),('risk','Distanza stop'),('pair','Coppie')]
        for sec,title in labels:
            lines += ['',f"<b>{title}</b>"]
            keys=sorted(set(s[sec]['dev'])|set(s[sec]['hold']))
            for k in keys:
                safe=str(k).replace('<','‹').replace('>','›')
                a=s[sec]['dev'].get(k,{'n':0,'pf':0,'exp':0});b=s[sec]['hold'].get(k,{'n':0,'pf':0,'exp':0})
                lines.append(f"• {safe}: DEV n={a['n']} PF {a['pf']:.2f} Exp €{a['exp']:+.3f} · HOLD n={b['n']} PF {b['pf']:.2f} Exp €{b['exp']:+.3f}")
        if s.get('cached_resend'):lines += ['', 'ℹ️ Risultato recuperato dal database: audit non ricalcolato.']
        lines.append("\n⚠️ Breakdown descrittivo su bucket fissati a priori. Nessuna selezione dal holdout e nessuna modifica al live.")
        return '\n'.join(lines)
