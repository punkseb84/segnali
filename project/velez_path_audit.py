"""Read-only path/cost audit for closed Velez 15m PAPER trades.

Measures MFE/MAE, time to R thresholds, EMA20 failure after profit, and actual
recorded exit economics using current Binance PAPER costs. Never changes live rules.
"""
from __future__ import annotations

import json
import statistics
from typing import Any

import pandas as pd


class VelezPathAudit:
    LEVELS = (0.50, 1.00, 1.50, 2.00)

    def __init__(self, postgres: Any, logger: Any, *, notional_eur: float,
                 buy_fee_rate: float, sell_fee_rate: float,
                 spread_rate: float, slippage_rate: float) -> None:
        self.postgres = postgres
        self.logger = logger
        self.notional_eur = float(notional_eur)
        self.buy_fee_rate = float(buy_fee_rate)
        self.sell_fee_rate = float(sell_fee_rate)
        self.spread_rate = float(spread_rate)
        self.slippage_rate = float(slippage_rate)

    @staticmethod
    def _state(raw: Any) -> dict[str, Any]:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        return dict(raw or {}) if isinstance(raw, dict) else {}

    def _trades(self, limit: int = 300) -> list[dict[str, Any]]:
        rows = self.postgres.fetch_all(
            """SELECT id,pair,entry,stop_loss,reference_candle_time,created_at,telegram_sent_at,
                      closed_at,COALESCE(exchange,'Kraken'),COALESCE(score_breakdown,'{}'::jsonb),
                      status,COALESCE(outcome_resolution,''),outcome_price,
                      COALESCE(entry_notional_eur,0)
                 FROM signals.generated_signals
                WHERE telegram_sent_at IS NOT NULL
                  AND (LOWER(COALESCE(strategy,'')) LIKE %s
                       OR COALESCE(score_breakdown->>'runtime_version','') LIKE %s)
                  AND closed_at IS NOT NULL
                  AND status NOT IN ('NEW','OPEN','EXPIRED')
                ORDER BY created_at ASC LIMIT %s""",
            ("%velez%", "VELEZ%", limit),
        )
        out = []
        for r in rows:
            state = self._state(r[9])
            out.append({
                "id": int(r[0]), "pair": str(r[1]), "entry": float(r[2]), "stop": float(r[3]),
                "reference": r[4] or r[6] or r[5], "created": r[5], "closed": r[7],
                "exchange": str(r[8]), "state": state, "status": str(r[10]),
                "resolution": str(r[11]), "outcome_price": float(r[12]) if r[12] is not None else None,
                "notional": float(r[13]) if float(r[13] or 0) > 0 else self.notional_eur,
                "direction": str(state.get("direction", "LONG")).upper(),
            })
        return out

    def _frame(self, t: dict[str, Any]) -> pd.DataFrame:
        start = pd.Timestamp(t["reference"])
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        end = pd.Timestamp(t["closed"])
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
        warmup = start.floor("15min") - pd.Timedelta(minutes=15*80)
        rows = self.postgres.fetch_all(
            """SELECT timestamp,open,high,low,close FROM market_data.ohlc
                WHERE LOWER(TRIM(exchange))=LOWER(TRIM(%s)) AND pair=%s AND timeframe='15m'
                  AND timestamp>=%s AND timestamp<=%s ORDER BY timestamp ASC""",
            (t["exchange"], t["pair"], warmup.to_pydatetime(), end.to_pydatetime()),
        )
        f = pd.DataFrame(rows, columns=["timestamp","open","high","low","close"])
        if f.empty:
            return f
        f["timestamp"] = pd.to_datetime(f["timestamp"], utc=True)
        for c in ("open","high","low","close"):
            f[c] = pd.to_numeric(f[c], errors="coerce")
        f = f.dropna().reset_index(drop=True)
        f["ema20"] = f["close"].ewm(span=20, adjust=False).mean()
        return f[f["timestamp"] > start.floor("15min")].reset_index(drop=True)

    def _economics(self, t: dict[str, Any]) -> dict[str, float]:
        exit_price = t["outcome_price"]
        if exit_price is None or t["entry"] <= 0:
            return {"gross":0.0,"cost":0.0,"net":0.0,"net_r":0.0,"cost_r":0.0}
        qty = t["notional"] / t["entry"]
        gross = qty * ((exit_price-t["entry"]) if t["direction"] == "LONG" else (t["entry"]-exit_price))
        entry_fee = t["notional"] * self.buy_fee_rate
        exit_notional = qty * exit_price
        exit_fee = exit_notional * self.sell_fee_rate
        spread = qty * (t["entry"] + exit_price) * (self.spread_rate/2.0)
        slippage = qty * (t["entry"] + exit_price) * self.slippage_rate
        cost = entry_fee + exit_fee + spread + slippage
        risk_eur = qty * abs(t["entry"]-t["stop"])
        return {"gross":gross,"cost":cost,"net":gross-cost,
                "net_r":(gross-cost)/risk_eur if risk_eur>0 else 0.0,
                "cost_r":cost/risk_eur if risk_eur>0 else 0.0}

    def _path(self, t: dict[str, Any], f: pd.DataFrame) -> dict[str, Any]:
        risk = abs(t["entry"]-t["stop"])
        if risk <= 0 or f.empty:
            return {"mfe":0.0,"mae":0.0,"levels":{},"ema20_fail_after":{}}
        long = t["direction"] == "LONG"
        mfe = mae = 0.0
        first_hit: dict[float, int | None] = {x:None for x in self.LEVELS}
        ema_fail: dict[float, bool] = {x:False for x in self.LEVELS}
        for i, row in f.iterrows():
            high, low, close, ema20 = map(float, (row["high"],row["low"],row["close"],row["ema20"]))
            fav = (high-t["entry"])/risk if long else (t["entry"]-low)/risk
            adv = (t["entry"]-low)/risk if long else (high-t["entry"])/risk
            mfe, mae = max(mfe, fav), max(mae, adv)
            for level in self.LEVELS:
                if first_hit[level] is None and fav >= level:
                    first_hit[level] = i + 1
                if first_hit[level] is not None:
                    if (long and close < ema20) or ((not long) and close > ema20):
                        ema_fail[level] = True
        return {"mfe":mfe,"mae":mae,"levels":first_hit,"ema20_fail_after":ema_fail}

    def run(self) -> dict[str, Any]:
        self.logger.info("VELEZ_PATH_AUDIT_START")
        rows = []
        for t in self._trades():
            try:
                f = self._frame(t)
                p = self._path(t, f)
                e = self._economics(t)
                rows.append({**t, **p, **e})
                self.logger.info("VELEZ_PATH_TRADE id=%s pair=%s mfe=%.2f mae=%.2f gross=%.3f cost=%.3f net=%.3f cost_r=%.2f",
                                 t["id"],t["pair"],p["mfe"],p["mae"],e["gross"],e["cost"],e["net"],e["cost_r"])
            except Exception:
                self.logger.exception("VELEZ_PATH_TRADE_FAILED id=%s", t.get("id"))
        if not rows:
            return {"trades":0}

        threshold = {}
        for level in self.LEVELS:
            hit = [r for r in rows if r["levels"].get(level) is not None]
            threshold[level] = {
                "n": len(hit),
                "pct": 100.0*len(hit)/len(rows),
                "median_bars": statistics.median([r["levels"][level] for r in hit]) if hit else 0.0,
                "ema_fail_pct": 100.0*sum(1 for r in hit if r["ema20_fail_after"][level])/len(hit) if hit else 0.0,
                "median_peak": statistics.median([r["mfe"] for r in hit]) if hit else 0.0,
            }
        wins = [r for r in rows if r["net"] > 0]
        losses = [r for r in rows if r["net"] < 0]
        summary = {
            "trades":len(rows), "wins":len(wins), "losses":len(losses),
            "gross":sum(r["gross"] for r in rows), "cost":sum(r["cost"] for r in rows),
            "net":sum(r["net"] for r in rows),
            "avg_cost_r":statistics.mean([r["cost_r"] for r in rows]),
            "median_cost_r":statistics.median([r["cost_r"] for r in rows]),
            "mfe_avg":statistics.mean([r["mfe"] for r in rows]),
            "mfe_median":statistics.median([r["mfe"] for r in rows]),
            "mae_avg":statistics.mean([r["mae"] for r in rows]),
            "threshold":threshold,
        }
        self.logger.warning("VELEZ_PATH_AUDIT_SUMMARY %s", summary)
        return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        if not s.get("trades"):
            return "🧭 <b>AUDIT VELEZ · PERCORSO E COSTI</b>\n\nNessun trade chiuso disponibile."
        lines = [
            "🧭 <b>AUDIT VELEZ · PERCORSO E COSTI</b>",
            f"Trade: <b>{s['trades']}</b> · net+ <b>{s['wins']}</b> · net- <b>{s['losses']}</b>",
            f"P&L lordo ricostruito: <b>€{s['gross']:+.2f}</b>",
            f"Costi ricostruiti: <b>€{s['cost']:.2f}</b>",
            f"P&L netto ricostruito: <b>€{s['net']:+.2f}</b>",
            f"Costo medio: <b>{s['avg_cost_r']:.2f}R</b> · mediano <b>{s['median_cost_r']:.2f}R</b>",
            f"MFE medio/mediano: <b>+{s['mfe_avg']:.2f}R / +{s['mfe_median']:.2f}R</b>",
            f"MAE medio: <b>-{s['mae_avg']:.2f}R</b>",
            "", "📈 <b>Soglie raggiunte</b>",
        ]
        for level in (0.50,1.00,1.50,2.00):
            x = s["threshold"][level]
            lines.append(f"• +{level:.2f}R: <b>{x['n']}/{s['trades']} ({x['pct']:.1f}%)</b> · tempo mediano <b>{x['median_bars']:.0f} barre</b> · picco mediano <b>+{x['median_peak']:.2f}R</b> · poi perdita EMA20 <b>{x['ema_fail_pct']:.1f}%</b>")
        lines.append("\n⚠️ Solo diagnostica: nessuna regola live è stata modificata. Economia ricalcolata da outcome_price con fee/spread/slippage del runtime corrente.")
        return "\n".join(lines)
