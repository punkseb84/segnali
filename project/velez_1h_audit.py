"""One-shot historical audit of the Velez setup on 1h candles.

Uses the same technical structure as the live 15m scanner but does not change live trading.
Reports technical first-touch outcomes at +1R/+1.5R/+2R before -1R and net economics using
current modeled Binance costs. DB marker prevents reruns on Railway restarts.
"""
from __future__ import annotations

from typing import Any
import json
import statistics

import pandas as pd

AUDIT_VERSION = "VELEZ_1H_AUDIT_V1_20260808"


class Velez1hAudit:
    TARGETS = (1.0, 1.5, 2.0)
    HORIZONS = (4, 8, 12, 24)

    def __init__(
        self,
        postgres: Any,
        logger: Any,
        pairs: list[str],
        exchange: str = "kraken",
        notional_eur: float = 25.0,
        buy_fee_rate: float = 0.0016,
        sell_fee_rate: float = 0.0016,
        spread_rate: float = 0.0005,
        slippage_rate: float = 0.0,
        pullback_bars: int = 3,
        max_bars_per_pair: int = 1800,
    ) -> None:
        self.postgres = postgres
        self.logger = logger
        self.pairs = list(pairs)
        self.exchange = str(exchange)
        self.notional_eur = float(notional_eur)
        self.buy_fee_rate = float(buy_fee_rate)
        self.sell_fee_rate = float(sell_fee_rate)
        self.spread_rate = float(spread_rate)
        self.slippage_rate = float(slippage_rate)
        self.pullback_bars = max(2, int(pullback_bars))
        self.max_bars_per_pair = max(400, int(max_bars_per_pair))

    def _ensure_marker(self) -> None:
        self.postgres.execute(
            """CREATE TABLE IF NOT EXISTS statistics.velez_candidate_audit_runs (
                   version TEXT PRIMARY KEY,
                   completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                   summary JSONB NOT NULL DEFAULT '{}'::jsonb
               )"""
        )

    def already_done(self) -> bool:
        self._ensure_marker()
        rows = self.postgres.fetch_all(
            "SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1",
            (AUDIT_VERSION,),
        )
        return bool(rows)

    def _save_marker(self, summary: dict[str, Any]) -> None:
        self.postgres.execute(
            """INSERT INTO statistics.velez_candidate_audit_runs(version,summary)
               VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING""",
            (AUDIT_VERSION, json.dumps(summary, default=str)),
        )

    def _frame(self, pair: str) -> pd.DataFrame:
        rows = self.postgres.fetch_all(
            """SELECT timestamp,open,high,low,close,volume FROM (
                   SELECT timestamp,open,high,low,close,volume
                     FROM market_data.ohlc
                    WHERE LOWER(TRIM(exchange))=LOWER(TRIM(%s))
                      AND pair=%s AND timeframe='1h'
                    ORDER BY timestamp DESC LIMIT %s
               ) q ORDER BY timestamp ASC""",
            (self.exchange, pair, self.max_bars_per_pair),
        )
        d = pd.DataFrame(rows, columns=["timestamp","open","high","low","close","volume"])
        if d.empty:
            return d
        d["timestamp"] = pd.to_datetime(d["timestamp"], utc=True)
        for c in ("open","high","low","close","volume"):
            d[c] = pd.to_numeric(d[c], errors="coerce")
        d = d.dropna().reset_index(drop=True)
        d["ema20"] = d["close"].ewm(span=20, adjust=False).mean()
        d["ema200"] = d["close"].ewm(span=200, adjust=False).mean()
        return d

    @staticmethod
    def _first_touch(direction: str, entry: float, risk: float, future: pd.DataFrame, target_r: float, horizon: int) -> tuple[str, float | None]:
        tp = entry + target_r*risk if direction == "LONG" else entry - target_r*risk
        sl = entry - risk if direction == "LONG" else entry + risk
        for _, row in future.head(horizon).iterrows():
            hi, lo = float(row["high"]), float(row["low"])
            hit_tp = hi >= tp if direction == "LONG" else lo <= tp
            hit_sl = lo <= sl if direction == "LONG" else hi >= sl
            if hit_tp and hit_sl:
                return "AMBIGUOUS", None
            if hit_sl:
                return "SL", sl
            if hit_tp:
                return "TP", tp
        return "NONE", None

    def _net(self, direction: str, entry: float, exit_price: float) -> float:
        qty = self.notional_eur / entry
        gross = qty*((exit_price-entry) if direction == "LONG" else (entry-exit_price))
        entry_fee = self.notional_eur*self.buy_fee_rate
        exit_notional = qty*exit_price
        exit_fee = exit_notional*self.sell_fee_rate
        spread = qty*(entry+exit_price)*(self.spread_rate/2.0)
        slippage = qty*(entry+exit_price)*self.slippage_rate
        return gross-entry_fee-exit_fee-spread-slippage

    def _qualified(self, pair: str, d: pd.DataFrame) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if len(d) < 230:
            return rows
        start = max(205, self.pullback_bars+3)
        end = len(d)-max(self.HORIZONS)
        for i in range(start, end):
            latest, prev = d.iloc[i], d.iloc[i-1]
            pull = d.iloc[i-self.pullback_bars:i]
            close = float(latest["close"])
            e20, e20_2, e200 = float(latest["ema20"]), float(d.iloc[i-2]["ema20"]), float(latest["ema200"])
            long = close > e20 > e200 and e20 > e20_2
            short = close < e20 < e200 and e20 < e20_2
            if not long and not short:
                continue
            direction = "LONG" if long else "SHORT"
            if direction == "LONG":
                ordered = all(float(pull.iloc[j]["high"]) >= float(pull.iloc[j+1]["high"]) for j in range(len(pull)-1))
                touched = bool((pull["low"] <= pull["ema20"]).any())
                held = bool((pull["close"] > pull["ema200"]).all())
                trigger = float(latest["close"]) > float(latest["open"]) and float(latest["high"]) > float(prev["high"]) and close > float(prev["high"])
                stop = float(pull["low"].min()) - close*0.0005
            else:
                ordered = all(float(pull.iloc[j]["low"]) <= float(pull.iloc[j+1]["low"]) for j in range(len(pull)-1))
                touched = bool((pull["high"] >= pull["ema20"]).any())
                held = bool((pull["close"] < pull["ema200"]).all())
                trigger = float(latest["close"]) < float(latest["open"]) and float(latest["low"]) < float(prev["low"]) and close < float(prev["low"])
                stop = float(pull["high"].max()) + close*0.0005
            if not (ordered and touched and held and trigger):
                continue
            risk = abs(close-stop)
            if risk <= 0 or risk/close > 0.03:
                continue
            future = d.iloc[i+1:i+1+max(self.HORIZONS)]
            item = {
                "pair":pair,"time":latest["timestamp"],"direction":direction,
                "entry":close,"risk":risk,"risk_pct":100.0*risk/close,
                "hour":int(latest["timestamp"].hour),
            }
            for target in self.TARGETS:
                for horizon in self.HORIZONS:
                    outcome, exit_price = self._first_touch(direction, close, risk, future, target, horizon)
                    item[f"t{target}_h{horizon}"] = outcome
                    if horizon == 24 and outcome in {"TP","SL"} and exit_price is not None:
                        item[f"net_t{target}"] = self._net(direction, close, exit_price)
            rows.append(item)
        return rows

    @staticmethod
    def _technical_stats(rows: list[dict[str, Any]], target: float, horizon: int) -> dict[str, float]:
        key=f"t{target}_h{horizon}"
        resolved=[r for r in rows if r.get(key) in {"TP","SL"}]
        wins=sum(r[key]=="TP" for r in resolved)
        losses=len(resolved)-wins
        wr=wins/len(resolved) if resolved else 0.0
        pf=(wins*target)/losses if losses else (999.0 if wins else 0.0)
        exp=wr*target-(1-wr)
        return {"resolved":len(resolved),"wr":100*wr,"pf_r":pf,"exp_r":exp}

    @staticmethod
    def _net_stats(rows: list[dict[str, Any]], target: float) -> dict[str, float]:
        vals=[float(r[f"net_t{target}"]) for r in rows if f"net_t{target}" in r]
        wins=[v for v in vals if v>0]; losses=[v for v in vals if v<0]
        gp=sum(wins); gl=abs(sum(losses))
        return {
            "n":len(vals),"net":sum(vals),"pf":gp/gl if gl else (999.0 if gp else 0.0),
            "wr_net":100*len(wins)/len(vals) if vals else 0.0,
            "exp_eur":statistics.mean(vals) if vals else 0.0,
        }

    def run(self) -> dict[str, Any]:
        if self.already_done():
            return {"skipped":True,"version":AUDIT_VERSION}
        rows: list[dict[str, Any]]=[]; coverage=[]
        for pair in self.pairs:
            try:
                d=self._frame(pair)
                coverage.append({"pair":pair,"bars":len(d)})
                part=self._qualified(pair,d)
                rows.extend(part)
                self.logger.info("VELEZ_1H_AUDIT_PAIR pair=%s bars=%s qualified=%s",pair,len(d),len(part))
            except Exception:
                self.logger.exception("VELEZ_1H_AUDIT_PAIR_FAILED pair=%s",pair)
        rows=sorted(rows,key=lambda r:r["time"])
        tech={str(t):{str(h):self._technical_stats(rows,t,h) for h in self.HORIZONS} for t in self.TARGETS}
        net={str(t):self._net_stats(rows,t) for t in self.TARGETS}
        risk_pcts=[r["risk_pct"] for r in rows]
        cost_rate=self.buy_fee_rate+self.sell_fee_rate+self.spread_rate+2*self.slippage_rate
        median_risk_pct=statistics.median(risk_pcts) if risk_pcts else 0.0
        cost_r=(100*cost_rate/median_risk_pct) if median_risk_pct>0 else 0.0
        summary={
            "version":AUDIT_VERSION,"qualified":len(rows),"coverage":coverage,"tech":tech,"net":net,
            "median_risk_pct":median_risk_pct,"cost_rate_pct":100*cost_rate,"cost_r_median":cost_r,
        }
        self._save_marker(summary)
        return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        if s.get("skipped"): return ""
        usable=[x for x in s.get("coverage",[]) if x.get("bars",0)>=230]
        lines=[
            "🕐 <b>AUDIT VELEZ · TIMEFRAME 1H</b>",
            f"Setup qualificati: <b>{s.get('qualified',0)}</b> · coppie con storia sufficiente: <b>{len(usable)}</b>",
            f"Stop mediano: <b>{s.get('median_risk_pct',0):.3f}%</b> · costo round-trip: <b>{s.get('cost_rate_pct',0):.3f}%</b> ≈ <b>{s.get('cost_r_median',0):.2f}R</b>",
            "",
            "🎯 <b>First-touch tecnico · target prima di -1R</b>",
        ]
        for t in (1.0,1.5,2.0):
            x=s.get("tech",{}).get(str(t),{}).get("24",{})
            lines.append(f"• +{t:.1f}R entro 24h: n=<b>{x.get('resolved',0)}</b> · WR <b>{x.get('wr',0):.1f}%</b> · PF-R <b>{x.get('pf_r',0):.2f}</b> · Exp <b>{x.get('exp_r',0):+.3f}R</b>")
        lines += ["", "💶 <b>Economia netta · €25/trade · fee/spread correnti</b>"]
        for t in (1.0,1.5,2.0):
            x=s.get("net",{}).get(str(t),{})
            lines.append(f"• TP {t:.1f}R: n=<b>{x.get('n',0)}</b> · net+ <b>{x.get('wr_net',0):.1f}%</b> · PF <b>{x.get('pf',0):.2f}</b> · P&L <b>€{x.get('net',0):+.2f}</b> · Exp <b>€{x.get('exp_eur',0):+.3f}</b>")
        lines += ["", "⏱ <b>Velocità +1R</b>"]
        for h in (4,8,12,24):
            x=s.get("tech",{}).get("1.0",{}).get(str(h),{})
            lines.append(f"• entro {h}h: risolti <b>{x.get('resolved',0)}</b> · successo <b>{x.get('wr',0):.1f}%</b>")
        if usable:
            lines += ["", "📚 <b>Copertura 1h utilizzabile</b>"]
            lines.append("• "+", ".join(f"{x['pair']} ({x['bars']})" for x in usable[:10]))
            if len(usable)>10: lines.append(f"• + altre {len(usable)-10} coppie")
        lines.append("\n⚠️ Audit one-shot, nessuna modifica al live 15m. I doppi tocchi TP/SL nella stessa candela 1h sono esclusi. Il confronto va interpretato anche alla luce della diversa copertura storica 1h disponibile.")
        return "\n".join(lines)
