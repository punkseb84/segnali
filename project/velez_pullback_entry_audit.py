"""One-shot Velez 15m pullback-entry economics audit.

Compares the original market-on-trigger-close entry with passive limit entries placed after
an otherwise-qualified Velez trigger: 25% and 50% retracement of the trigger candle range,
and trigger-time EMA20. Limit orders may fill during the next 4 closed 15m bars. The original
technical stop is preserved. Diagnostic only; no live trading changes.
"""
from __future__ import annotations

from typing import Any
import statistics

from project.velez_candidate_audit import VelezCandidateAudit

AUDIT_VERSION = "VELEZ_PULLBACK_ENTRY_V1_20260808"


class VelezPullbackEntryAudit(VelezCandidateAudit):
    TARGETS = (1.0, 1.5, 2.0)
    FILL_BARS = 4
    OUTCOME_BARS = 24

    def __init__(self, *args: Any, notional_eur: float = 25.0,
                 maker_fee_rate: float = 0.0007, taker_fee_rate: float = 0.0016,
                 spread_rate: float = 0.0005, slippage_rate: float = 0.0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.notional = float(notional_eur)
        self.maker_fee = float(maker_fee_rate)
        self.taker_fee = float(taker_fee_rate)
        self.spread = float(spread_rate)
        self.slippage = float(slippage_rate)

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

    def _net(self, direction: str, entry: float, exit_price: float, *, maker_entry: bool) -> float:
        qty = self.notional / entry
        gross = qty * ((exit_price-entry) if direction == "LONG" else (entry-exit_price))
        entry_fee = self.notional * (self.maker_fee if maker_entry else self.taker_fee)
        exit_notional = qty * exit_price
        exit_fee = exit_notional * self.taker_fee
        # Conservative: market/taker exit pays half the modeled round-trip spread; passive entry pays none.
        spread_cost = qty * exit_price * (self.spread/2.0 if maker_entry else self.spread)
        slip = qty * (entry + exit_price) * self.slippage
        return gross - entry_fee - exit_fee - spread_cost - slip

    @staticmethod
    def _first_touch(direction: str, entry: float, stop: float, target_r: float, future: Any) -> tuple[str, float | None]:
        risk = abs(entry-stop)
        if risk <= 0:
            return "INVALID", None
        tp = entry + target_r*risk if direction == "LONG" else entry-target_r*risk
        for _, r in future.iterrows():
            hi, lo = float(r["high"]), float(r["low"])
            hit_tp = hi >= tp if direction == "LONG" else lo <= tp
            hit_sl = lo <= stop if direction == "LONG" else hi >= stop
            if hit_tp and hit_sl:
                return "AMBIGUOUS", None
            if hit_sl:
                return "SL", stop
            if hit_tp:
                return "TP", tp
        return "NONE", None

    def _fill_limit(self, direction: str, level: float, stop: float, future: Any) -> tuple[int | None, bool]:
        """Return fill-bar offset and whether stop invalidated setup before fill."""
        for j, (_, r) in enumerate(future.head(self.FILL_BARS).iterrows(), start=1):
            hi, lo = float(r["high"]), float(r["low"])
            stop_hit = lo <= stop if direction == "LONG" else hi >= stop
            fill = lo <= level if direction == "LONG" else hi >= level
            if stop_hit and fill:
                # Intrabar order unknowable: conservatively reject rather than count a favorable fill.
                return None, True
            if stop_hit:
                return None, True
            if fill:
                return j, False
        return None, False

    def _rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for pair in self.pairs:
            d = self._frame(pair)
            if len(d) < 230:
                continue
            start = max(205, self.pullback_bars + 3)
            end = len(d) - (self.FILL_BARS + self.OUTCOME_BARS)
            for i in range(start, end):
                latest, prev = d.iloc[i], d.iloc[i-1]
                pull = d.iloc[i-self.pullback_bars:i]
                e20 = float(latest["ema20"]); e20_2 = float(d.iloc[i-2]["ema20"]); e200 = float(latest["ema200"])
                close = float(latest["close"]); op = float(latest["open"]); hi = float(latest["high"]); lo = float(latest["low"])
                long = close > e20 > e200 and e20 > e20_2
                short = close < e20 < e200 and e20 < e20_2
                if not long and not short:
                    continue
                direction = "LONG" if long else "SHORT"
                if long:
                    ordered = all(float(pull.iloc[j]["high"]) >= float(pull.iloc[j+1]["high"]) for j in range(len(pull)-1))
                    touched = bool((pull["low"] <= pull["ema20"]).any()); held = bool((pull["close"] > pull["ema200"]).all())
                    trigger = close > op and hi > float(prev["high"]) and close > float(prev["high"])
                    stop = float(pull["low"].min()) - close*0.0005
                else:
                    ordered = all(float(pull.iloc[j]["low"]) <= float(pull.iloc[j+1]["low"]) for j in range(len(pull)-1))
                    touched = bool((pull["high"] >= pull["ema20"]).any()); held = bool((pull["close"] < pull["ema200"]).all())
                    trigger = close < op and lo < float(prev["low"]) and close < float(prev["low"])
                    stop = float(pull["high"].max()) + close*0.0005
                if not (ordered and touched and held and trigger):
                    continue
                if abs(close-stop) <= 0 or abs(close-stop)/close > 0.03:
                    continue

                candle_range = max(hi-lo, 1e-12)
                levels = {
                    "MARKET_CLOSE": close,
                    "LIMIT_25": close - 0.25*candle_range if direction == "LONG" else close + 0.25*candle_range,
                    "LIMIT_50": close - 0.50*candle_range if direction == "LONG" else close + 0.50*candle_range,
                    "LIMIT_EMA20": e20,
                }
                future_all = d.iloc[i+1:i+1+self.FILL_BARS+self.OUTCOME_BARS]
                row: dict[str, Any] = {"pair":pair,"time":latest["timestamp"],"direction":direction,"hour":int(latest["timestamp"].hour)}
                for name, entry in levels.items():
                    maker = name != "MARKET_CLOSE"
                    if (direction == "LONG" and entry <= stop) or (direction == "SHORT" and entry >= stop):
                        row[name] = {"filled":False,"invalid":True,"fill_bar":None,"risk_pct":0.0,"target":{}}
                        continue
                    if maker:
                        fill_bar, invalid = self._fill_limit(direction, entry, stop, future_all)
                        filled = fill_bar is not None
                    else:
                        fill_bar, invalid, filled = 0, False, True
                    item = {"filled":filled,"invalid":invalid,"fill_bar":fill_bar,"risk_pct":100*abs(entry-stop)/entry,"target":{}}
                    if filled:
                        start_offset = int(fill_bar or 0)
                        future_out = future_all.iloc[start_offset:start_offset+self.OUTCOME_BARS]
                        for target in self.TARGETS:
                            outcome, px = self._first_touch(direction, entry, stop, target, future_out)
                            item["target"][str(target)] = {
                                "outcome":outcome,
                                "pnl":self._net(direction,entry,float(px),maker_entry=maker) if px is not None else None,
                            }
                    row[name] = item
                rows.append(row)
        return sorted(rows,key=lambda r:r["time"])

    @staticmethod
    def _stats(rows: list[dict[str, Any]], variant: str, target: float) -> dict[str, float]:
        filled=[r for r in rows if r[variant]["filled"]]
        vals=[]; technical=[]
        for r in filled:
            x=r[variant]["target"].get(str(target),{})
            if x.get("outcome") in {"TP","SL"} and x.get("pnl") is not None:
                vals.append(float(x["pnl"])); technical.append(x["outcome"])
        gp=sum(x for x in vals if x>0); gl=abs(sum(x for x in vals if x<0))
        return {
            "setups":len(rows),"filled":len(filled),"fill_rate":100*len(filled)/len(rows) if rows else 0.0,
            "n":len(vals),"tech_wr":100*sum(x=="TP" for x in technical)/len(technical) if technical else 0.0,
            "netpos":100*sum(x>0 for x in vals)/len(vals) if vals else 0.0,
            "pf":gp/gl if gl>0 else (999.0 if gp>0 else 0.0),"net":sum(vals),
            "exp":statistics.mean(vals) if vals else 0.0,
            "risk_med":statistics.median([r[variant]["risk_pct"] for r in filled]) if filled else 0.0,
            "fill_bar_med":statistics.median([r[variant]["fill_bar"] for r in filled if r[variant]["fill_bar"] is not None]) if filled else 0.0,
        }

    def run(self) -> dict[str, Any]:
        if self.already_done():
            return {"skipped":True,"version":AUDIT_VERSION}
        rows=self._rows(); cut=int(len(rows)*0.70); dev,hold=rows[:cut],rows[cut:]
        variants=("MARKET_CLOSE","LIMIT_25","LIMIT_50","LIMIT_EMA20")
        results={}
        for v in variants:
            results[v]={str(t):{"dev":self._stats(dev,v,t),"hold":self._stats(hold,v,t)} for t in self.TARGETS}
        robust=[]
        for v in variants[1:]:
            for t in self.TARGETS:
                d=results[v][str(t)]["dev"]; h=results[v][str(t)]["hold"]
                if d["n"]>=40 and h["n"]>=15 and d["pf"]>1 and h["pf"]>1 and d["exp"]>0 and h["exp"]>0:
                    robust.append({"variant":v,"target":t,"dev":d,"hold":h})
        summary={"version":AUDIT_VERSION,"rows":len(rows),"dev":len(dev),"hold":len(hold),"results":results,"robust":robust}
        self._save_marker(summary); return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        if s.get("skipped"): return ""
        labels={"MARKET_CLOSE":"Market trigger close","LIMIT_25":"Limit retrace 25%","LIMIT_50":"Limit retrace 50%","LIMIT_EMA20":"Limit EMA20"}
        lines=["🎣 <b>AUDIT VELEZ · INGRESSO SU PULLBACK</b>",f"Setup: <b>{s.get('rows',0)}</b> · sviluppo <b>{s.get('dev',0)}</b> · holdout <b>{s.get('hold',0)}</b>","Limit: attesa max <b>4 barre</b> dopo il trigger · stop tecnico originale invariato","Fee: market/taker <b>0,16%</b> · ingresso limit/maker <b>0,07%</b> · uscita taker <b>0,16%</b>",""]
        for v,targets in s.get("results",{}).items():
            lines.append(f"📌 <b>{labels.get(v,v)}</b>")
            for t,x in targets.items():
                d,h=x["dev"],x["hold"]
                lines.append(f"• TP {t}R · fill HOLD <b>{h['fill_rate']:.1f}%</b> · HOLD n={h['n']} WR tech <b>{h['tech_wr']:.1f}%</b> · PF netto <b>{h['pf']:.2f}</b> · P&L <b>€{h['net']:+.2f}</b> · Exp <b>€{h['exp']:+.3f}</b> · stop med <b>{h['risk_med']:.3f}%</b>")
            lines.append("")
        lines.append("✅ <b>Varianti positive sia DEV sia HOLD</b>")
        if not s.get("robust"):
            lines.append("• Nessuna variante limit testata mantiene PF netto maggiore di 1 ed expectancy positiva in entrambi i periodi.")
        else:
            for x in s["robust"]:
                d,h=x["dev"],x["hold"]
                lines.append(f"• {labels.get(x['variant'],x['variant'])} · TP {x['target']:.1f}R · DEV PF {d['pf']:.2f} Exp €{d['exp']:+.3f} · HOLD PF {h['pf']:.2f} Exp €{h['exp']:+.3f}")
        lines.append("\n⚠️ One-shot diagnostico. Nessuna modifica al live. Se stop e limit vengono toccati nella stessa candela prima del fill, il setup è scartato conservativamente.")
        return "\n".join(lines)
