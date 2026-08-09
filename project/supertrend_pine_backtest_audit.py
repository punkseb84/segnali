"""One-shot historical backtest of the user-provided Pine Supertrend script.

Pine logic reproduced:
    [supertrend, direction] = ta.supertrend(3.0, 10)
    BUY  = direction < 0 and direction[1] > 0
    SELL = direction > 0 and direction[1] < 0

The indicator itself does not define execution. To avoid look-ahead this audit
executes signals at the NEXT 15m bar open. It reports LONG-only and continuous
LONG/SHORT reversal interpretations, plus a €100 paper-portfolio view capped at
four simultaneous €25 positions. Fees/spread/slippage are included. No live
rules are changed.
"""
from __future__ import annotations

from typing import Any
import json
import math
import statistics
import pandas as pd
from project.velez_candidate_audit import VelezCandidateAudit

AUDIT_VERSION="PINE_SUPERTREND_10_3_BACKTEST_V2_20260809"
ATR_LENGTH=10
FACTOR=3.0
TIMEFRAME="15m"
PORTFOLIO_EUR=100.0
MAX_OPEN=4

class SupertrendPineBacktestAudit(VelezCandidateAudit):
    def __init__(self,*args:Any,notional_eur:float=25.0,buy_fee_rate:float=.0016,sell_fee_rate:float=.0016,spread_rate:float=.0005,slippage_rate:float=0.0,**kwargs:Any)->None:
        super().__init__(*args,**kwargs);self.notional=float(notional_eur);self.buy_fee=float(buy_fee_rate);self.sell_fee=float(sell_fee_rate);self.spread=float(spread_rate);self.slippage=float(slippage_rate)
    def already_done(self)->bool:
        self._ensure_marker();return bool(self.postgres.fetch_all("SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1",(AUDIT_VERSION,)))
    def _save_marker(self,s:dict[str,Any])->None:
        self.postgres.execute("INSERT INTO statistics.velez_candidate_audit_runs(version,summary) VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING",(AUDIT_VERSION,json.dumps(s,default=str)))

    @staticmethod
    def _rma_true_range(df:pd.DataFrame,length:int)->list[float]:
        high=df['high'].astype(float).tolist();low=df['low'].astype(float).tolist();close=df['close'].astype(float).tolist();tr=[]
        for i in range(len(df)):
            tr.append(high[i]-low[i] if i==0 else max(high[i]-low[i],abs(high[i]-close[i-1]),abs(low[i]-close[i-1])))
        atr=[math.nan]*len(df)
        if len(df)<length:return atr
        atr[length-1]=sum(tr[:length])/length
        for i in range(length,len(df)):atr[i]=(atr[i-1]*(length-1)+tr[i])/length
        return atr

    @classmethod
    def _supertrend_direction(cls,df:pd.DataFrame)->list[float]:
        n=len(df);atr=cls._rma_true_range(df,ATR_LENGTH);high=df['high'].astype(float).tolist();low=df['low'].astype(float).tolist();close=df['close'].astype(float).tolist();upper=[math.nan]*n;lower=[math.nan]*n;st=[math.nan]*n;direction=[math.nan]*n
        for i in range(n):
            if math.isnan(atr[i]):continue
            hl2=(high[i]+low[i])/2;bu=hl2+FACTOR*atr[i];bl=hl2-FACTOR*atr[i]
            if i==0 or math.isnan(upper[i-1]):upper[i]=bu;lower[i]=bl
            else:
                pu,pl,pc=upper[i-1],lower[i-1],close[i-1]
                lower[i]=bl if (bl>pl or pc<pl) else pl
                upper[i]=bu if (bu<pu or pc>pu) else pu
            if i==0 or math.isnan(atr[i-1]) or math.isnan(st[i-1]):direction[i]=1.0
            elif abs(st[i-1]-upper[i-1])<=max(1e-12,abs(st[i-1])*1e-12):direction[i]=-1.0 if close[i]>upper[i] else 1.0
            else:direction[i]=1.0 if close[i]<lower[i] else -1.0
            st[i]=lower[i] if direction[i]<0 else upper[i]
        return direction

    def _trade_pnl(self,direction:str,entry:float,exit_price:float)->tuple[float,float]:
        if entry<=0 or exit_price<=0:return 0.0,0.0
        qty=self.notional/entry;gross=qty*((exit_price-entry) if direction=='LONG' else (entry-exit_price));exit_notional=qty*exit_price
        costs=self.notional*self.buy_fee+exit_notional*self.sell_fee+(self.notional+exit_notional)*(self.spread/2+self.slippage)
        return gross,gross-costs

    @staticmethod
    def _stats(trades:list[dict[str,Any]])->dict[str,Any]:
        vals=[float(t['net']) for t in trades];gross=[float(t['gross']) for t in trades];wins=[v for v in vals if v>0];loss=[v for v in vals if v<0];gp=sum(wins);gl=abs(sum(loss));eq=peak=dd=0.0
        for t in sorted(trades,key=lambda x:x['exit_time']):eq+=float(t['net']);peak=max(peak,eq);dd=max(dd,peak-eq)
        return {'n':len(trades),'wr':100*len(wins)/len(trades) if trades else 0.0,'gross':sum(gross),'net':sum(vals),'pf':gp/gl if gl else (999.0 if gp else 0.0),'exp':statistics.mean(vals) if vals else 0.0,'max_dd':dd,'avg_bars':statistics.mean([t['bars'] for t in trades]) if trades else 0.0}

    @staticmethod
    def _portfolio_cap(trades:list[dict[str,Any]],max_open:int=MAX_OPEN)->tuple[list[dict[str,Any]],int]:
        accepted=[];active=[];rejected=0
        for t in sorted(trades,key=lambda x:(x['entry_time'],x['pair'],x['direction'])):
            active=[x for x in active if x['exit_time']>t['entry_time']]
            if len(active)>=max_open:rejected+=1;continue
            accepted.append(t);active.append(t)
        return accepted,rejected

    def _pair_trades(self,pair:str)->tuple[list[dict[str,Any]],list[dict[str,Any]],dict[str,Any]]:
        d=self._frame(pair).copy()
        if len(d)<ATR_LENGTH+5:return [],[],{'pair':pair,'bars':len(d),'buy_signals':0,'sell_signals':0}
        d=d.sort_values('timestamp').reset_index(drop=True);direction=self._supertrend_direction(d);signals=[]
        for i in range(1,len(d)-1):
            a,b=direction[i-1],direction[i]
            if math.isnan(a) or math.isnan(b):continue
            if b<0 and a>0:signals.append((i,'BUY'))
            elif b>0 and a<0:signals.append((i,'SELL'))
        long_trades=[];open_long=None
        for sig_i,sig in signals:
            j=sig_i+1;px=float(d.iloc[j]['open']);tm=d.iloc[j]['timestamp']
            if sig=='BUY' and open_long is None:open_long={'entry':px,'entry_i':j,'entry_time':tm}
            elif sig=='SELL' and open_long is not None:
                gross,net=self._trade_pnl('LONG',open_long['entry'],px);long_trades.append({'pair':pair,'direction':'LONG','entry':open_long['entry'],'exit':px,'entry_time':open_long['entry_time'],'exit_time':tm,'bars':j-open_long['entry_i'],'gross':gross,'net':net});open_long=None
        rev=[];pos=None
        for sig_i,sig in signals:
            j=sig_i+1;px=float(d.iloc[j]['open']);tm=d.iloc[j]['timestamp'];nd='LONG' if sig=='BUY' else 'SHORT'
            if pos is None:pos={'direction':nd,'entry':px,'entry_i':j,'entry_time':tm};continue
            if pos['direction']==nd:continue
            gross,net=self._trade_pnl(pos['direction'],pos['entry'],px);rev.append({'pair':pair,'direction':pos['direction'],'entry':pos['entry'],'exit':px,'entry_time':pos['entry_time'],'exit_time':tm,'bars':j-pos['entry_i'],'gross':gross,'net':net});pos={'direction':nd,'entry':px,'entry_i':j,'entry_time':tm}
        return long_trades,rev,{'pair':pair,'bars':len(d),'from':str(d.iloc[0]['timestamp']),'to':str(d.iloc[-1]['timestamp']),'buy_signals':sum(s=='BUY' for _,s in signals),'sell_signals':sum(s=='SELL' for _,s in signals)}

    def run(self)->dict[str,Any]:
        if self.already_done():return {'skipped':True,'version':AUDIT_VERSION}
        all_long=[];all_rev=[];coverage=[];pairs={}
        for pair in self.pairs[:20]:
            lt,rt,info=self._pair_trades(pair);all_long.extend(lt);all_rev.extend(rt);coverage.append(info);pairs[pair]={'long':self._stats(lt),'reversal':self._stats(rt),'coverage':info}
        cap_long,reject_long=self._portfolio_cap(all_long);cap_rev,reject_rev=self._portfolio_cap(all_rev)
        s={'version':AUDIT_VERSION,'timeframe':TIMEFRAME,'atr_length':ATR_LENGTH,'factor':FACTOR,'pairs':list(self.pairs[:20]),'notional':self.notional,'portfolio_eur':PORTFOLIO_EUR,'max_open':MAX_OPEN,'buy_fee':self.buy_fee,'sell_fee':self.sell_fee,'spread':self.spread,'slippage':self.slippage,'execution':'NEXT_BAR_OPEN','long':self._stats(all_long),'reversal':self._stats(all_rev),'portfolio_long':self._stats(cap_long),'portfolio_reversal':self._stats(cap_rev),'rejected_long':reject_long,'rejected_reversal':reject_rev,'pair_results':pairs,'coverage':coverage}
        self._save_marker(s);return s

    @staticmethod
    def format_report(s:dict[str,Any])->str:
        if s.get('skipped'):return ''
        l=s.get('long',{});r=s.get('reversal',{});pl=s.get('portfolio_long',{});pr=s.get('portfolio_reversal',{})
        lines=['📈 <b>BACKTEST PINE · SUPERTREND 10×3</b>',f"Timeframe: <b>{s.get('timeframe','15m')}</b> · crypto: <b>{len(s.get('pairs',[]))}</b> · esecuzione: <b>open barra successiva</b>",f"ATR <b>{s.get('atr_length',10)}</b> · multiplier <b>{s.get('factor',3):.1f}</b> · €{s.get('notional',25):.2f}/trade",f"Costi: fee {100*s.get('buy_fee',0):.3f}% + {100*s.get('sell_fee',0):.3f}% · spread {100*s.get('spread',0):.3f}%",'', '🟢 <b>LONG-only · tutti i segnali</b>',f"Trade {l.get('n',0)} · WR {l.get('wr',0):.1f}% · PF {l.get('pf',0):.2f} · lordo €{l.get('gross',0):+.2f} · netto €{l.get('net',0):+.2f}",f"Exp €{l.get('exp',0):+.3f}/trade · DD €{l.get('max_dd',0):.2f} · durata {l.get('avg_bars',0):.1f} barre",'', '🔄 <b>REVERSAL LONG/SHORT · tutti i segnali</b>',f"Trade {r.get('n',0)} · WR {r.get('wr',0):.1f}% · PF {r.get('pf',0):.2f} · lordo €{r.get('gross',0):+.2f} · netto €{r.get('net',0):+.2f}",f"Exp €{r.get('exp',0):+.3f}/trade · DD €{r.get('max_dd',0):.2f} · durata {r.get('avg_bars',0):.1f} barre",'', '💼 <b>Portafoglio €100 · max 4 × €25</b>',f"LONG-only: trade eseguiti {pl.get('n',0)} · scartati per capitale {s.get('rejected_long',0)} · PF {pl.get('pf',0):.2f} · P&L €{pl.get('net',0):+.2f} · capitale teorico €{100+pl.get('net',0):.2f}",f"Reversal: trade eseguiti {pr.get('n',0)} · scartati per capitale {s.get('rejected_reversal',0)} · PF {pr.get('pf',0):.2f} · P&L €{pr.get('net',0):+.2f} · capitale teorico €{100+pr.get('net',0):.2f}",'','📦 <b>LONG-only per coppia</b>']
        for pair,x in sorted(s.get('pair_results',{}).items(),key=lambda kv:kv[1]['long']['net'],reverse=True):
            st=x['long'];lines.append(f"• {pair}: n={st['n']} · WR {st['wr']:.1f}% · PF {st['pf']:.2f} · netto €{st['net']:+.2f}")
        lines.append('\n⚠️ Il Pine originale è un indicatore: non contiene sizing, stop o take profit. Il test usa il segnale solo a candela chiusa e simula l’esecuzione all’open successivo. Nessuna modifica al live.')
        return '\n'.join(lines)
