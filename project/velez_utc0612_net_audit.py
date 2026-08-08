"""One-shot net profitability audit for Velez 15m UTC 06-12.
Compares all-hours baseline vs UTC 06-12 at TP 1R/1.5R/2R using current Binance fees/spread.
Diagnostic only; no live rule changes.
"""
from __future__ import annotations

from typing import Any
import statistics

from project.velez_edge_validation_audit import VelezEdgeValidationAudit

AUDIT_VERSION = "VELEZ_UTC0612_NET_V1_20260808"


class VelezUTC0612NetAudit(VelezEdgeValidationAudit):
    def __init__(self, *args: Any, notional_eur: float = 25.0, buy_fee_rate: float = 0.0016,
                 sell_fee_rate: float = 0.0016, spread_rate: float = 0.0005,
                 slippage_rate: float = 0.0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.notional_eur = float(notional_eur)
        self.buy_fee_rate = float(buy_fee_rate)
        self.sell_fee_rate = float(sell_fee_rate)
        self.spread_rate = float(spread_rate)
        self.slippage_rate = float(slippage_rate)

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

    def _qualified_with_prices(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for pair in self.pairs:
            d = self._frame(pair)
            if len(d) < 230:
                continue
            start = max(205, self.pullback_bars + 3)
            end = len(d) - self.HORIZON
            for i in range(start, end):
                latest, prev = d.iloc[i], d.iloc[i-1]
                pull = d.iloc[i-self.pullback_bars:i]
                e20, e20_2, e200 = float(latest['ema20']), float(d.iloc[i-2]['ema20']), float(latest['ema200'])
                close = float(latest['close'])
                long = close > e20 > e200 and e20 > e20_2
                short = close < e20 < e200 and e20 < e20_2
                if not long and not short:
                    continue
                direction = 'LONG' if long else 'SHORT'
                if long:
                    ordered = all(float(pull.iloc[j]['high']) >= float(pull.iloc[j+1]['high']) for j in range(len(pull)-1))
                    touched = bool((pull['low'] <= pull['ema20']).any())
                    held = bool((pull['close'] > pull['ema200']).all())
                    trigger = float(latest['close']) > float(latest['open']) and float(latest['high']) > float(prev['high']) and close > float(prev['high'])
                    stop = float(pull['low'].min()) - close*0.0005
                else:
                    ordered = all(float(pull.iloc[j]['low']) <= float(pull.iloc[j+1]['low']) for j in range(len(pull)-1))
                    touched = bool((pull['high'] >= pull['ema20']).any())
                    held = bool((pull['close'] < pull['ema200']).all())
                    trigger = float(latest['close']) < float(latest['open']) and float(latest['low']) < float(prev['low']) and close < float(prev['low'])
                    stop = float(pull['high'].max()) + close*0.0005
                if not (ordered and touched and held and trigger):
                    continue
                risk = abs(close-stop)
                if risk <= 0 or risk/close > 0.03:
                    continue
                future = d.iloc[i+1:i+25]
                row = {'pair':pair,'time':latest['timestamp'],'direction':direction,'entry':close,'stop':stop,
                       'risk':risk,'risk_pct':100*risk/close,'hour':int(latest['timestamp'].hour)}
                for target in self.TARGETS:
                    row[f't{target}'] = self._target_touch(direction, close, risk, future, target)
                rows.append(row)
        return sorted(rows,key=lambda x:x['time'])

    def _net_for(self, row: dict[str, Any], target_r: float) -> float | None:
        outcome = row[f't{target_r}']
        if outcome not in {'TP','SL'}:
            return None
        entry, risk, direction = float(row['entry']), float(row['risk']), row['direction']
        exit_price = (entry + target_r*risk if direction=='LONG' else entry-target_r*risk) if outcome=='TP' else float(row['stop'])
        qty = self.notional_eur / entry
        gross = qty*((exit_price-entry) if direction=='LONG' else (entry-exit_price))
        entry_fee = self.notional_eur*self.buy_fee_rate
        exit_notional = qty*exit_price
        exit_fee = exit_notional*self.sell_fee_rate
        friction = qty*(entry+exit_price)*((self.spread_rate/2.0)+self.slippage_rate)
        return gross-entry_fee-exit_fee-friction

    def _net_stats(self, rows: list[dict[str, Any]], target: float) -> dict[str, float]:
        vals=[self._net_for(r,target) for r in rows]
        pnls=[float(x) for x in vals if x is not None]
        wins=[x for x in pnls if x>0]; losses=[x for x in pnls if x<0]
        gp, gl = sum(wins), abs(sum(losses))
        return {'n':len(pnls),'net_positive':100*len(wins)/len(pnls) if pnls else 0.0,
                'pf':gp/gl if gl>0 else (999.0 if gp>0 else 0.0),'pnl':sum(pnls),
                'exp':statistics.mean(pnls) if pnls else 0.0,
                'cost_r_median':statistics.median([((self.notional_eur*self.buy_fee_rate + self.notional_eur*self.sell_fee_rate + self.notional_eur*self.spread_rate)/ (self.notional_eur*(r['risk']/r['entry']))) for r in rows if r['risk']>0]) if rows else 0.0,
                'risk_pct_median':statistics.median([r['risk_pct'] for r in rows]) if rows else 0.0}

    def run(self) -> dict[str, Any]:
        if self.already_done(): return {'skipped':True,'version':AUDIT_VERSION}
        rows=self._qualified_with_prices(); utc=[r for r in rows if 6<=r['hour']<12]
        cut=int(len(rows)*0.70); hold=rows[cut:]; utc_hold=[r for r in hold if 6<=r['hour']<12]
        summary={'version':AUDIT_VERSION,'all_n':len(rows),'utc_n':len(utc),'hold_n':len(hold),'utc_hold_n':len(utc_hold),
                 'all':{},'utc':{},'utc_hold':{},'fees':{'buy':self.buy_fee_rate,'sell':self.sell_fee_rate,'spread':self.spread_rate}}
        for t in self.TARGETS:
            summary['all'][str(t)]=self._net_stats(rows,t)
            summary['utc'][str(t)]=self._net_stats(utc,t)
            summary['utc_hold'][str(t)]=self._net_stats(utc_hold,t)
        self._save_marker(summary); return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        if s.get('skipped'): return ''
        lines=["💶 <b>AUDIT VELEZ · UTC 06–12 NETTO</b>",
               f"Setup totali: <b>{s.get('all_n',0)}</b> · UTC 06–12: <b>{s.get('utc_n',0)}</b> · holdout UTC: <b>{s.get('utc_hold_n',0)}</b>",
               "","📊 <b>Tutti gli orari · netto</b>"]
        for t,x in s.get('all',{}).items():
            lines.append(f"• TP {t}R · n={x['n']} · net+ {x['net_positive']:.1f}% · PF <b>{x['pf']:.2f}</b> · P&L <b>€{x['pnl']:+.2f}</b> · Exp <b>€{x['exp']:+.3f}</b>")
        lines += ["","🕕 <b>UTC 06–12 · netto</b>"]
        for t,x in s.get('utc',{}).items():
            lines.append(f"• TP {t}R · n={x['n']} · net+ {x['net_positive']:.1f}% · PF <b>{x['pf']:.2f}</b> · P&L <b>€{x['pnl']:+.2f}</b> · Exp <b>€{x['exp']:+.3f}</b> · stop med {x['risk_pct_median']:.3f}% · costo≈{x['cost_r_median']:.2f}R")
        lines += ["","🧪 <b>Solo holdout UTC 06–12 · netto</b>"]
        for t,x in s.get('utc_hold',{}).items():
            lines.append(f"• TP {t}R · n={x['n']} · net+ {x['net_positive']:.1f}% · PF <b>{x['pf']:.2f}</b> · P&L <b>€{x['pnl']:+.2f}</b> · Exp <b>€{x['exp']:+.3f}</b>")
        lines.append("\n⚠️ One-shot diagnostico. Fee 0,16% entrata + 0,16% uscita e spread runtime inclusi. Nessuna modifica al live.")
        return '\n'.join(lines)
