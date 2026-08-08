"""Diagnostic audit for Velez 15m entry quality.

Read-only: this module never changes signals or live trading rules. It reconstructs
setup context around already-closed Velez trades and measures whether weak follow-
through is associated with slope, extension, trigger-bar geometry, pullback depth,
or the first four post-entry candles.
"""
from __future__ import annotations

import json
from typing import Any

import pandas as pd


class VelezEntryAudit:
    def __init__(self, postgres: Any, logger: Any) -> None:
        self.postgres = postgres
        self.logger = logger

    @staticmethod
    def _state(raw: Any) -> dict[str, Any]:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        return dict(raw or {}) if isinstance(raw, dict) else {}

    @staticmethod
    def _utc(value: Any) -> pd.Timestamp:
        ts = pd.Timestamp(value)
        return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")

    def _trades(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.postgres.fetch_all(
            """SELECT id,pair,entry,stop_loss,reference_candle_time,created_at,telegram_sent_at,
                      closed_at,COALESCE(exchange,'Kraken'),COALESCE(score_breakdown,'{}'::jsonb),
                      COALESCE(net_profit_tp1_eur,0),COALESCE(net_loss_sl_eur,0),status,
                      COALESCE(outcome_resolution,'')
                 FROM signals.generated_signals
                WHERE telegram_sent_at IS NOT NULL
                  AND (LOWER(COALESCE(strategy,'')) LIKE %s
                       OR COALESCE(score_breakdown->>'runtime_version','') LIKE %s)
                  AND closed_at IS NOT NULL
                  AND status NOT IN ('NEW','OPEN','EXPIRED')
                ORDER BY created_at ASC
                LIMIT %s""",
            ("%velez%", "VELEZ%", limit),
        )
        return [
            {
                "id": int(r[0]), "pair": str(r[1]), "entry": float(r[2]), "stop": float(r[3]),
                "reference": r[4] or r[6] or r[5], "created": r[5], "sent": r[6], "closed": r[7],
                "exchange": str(r[8]), "state": self._state(r[9]),
                "net": float(r[10] or 0)-float(r[11] or 0), "status": str(r[12]),
                "resolution": str(r[13]),
            }
            for r in rows
        ]

    def _context(self, trade: dict[str, Any]) -> pd.DataFrame:
        ref = self._utc(trade["reference"]).floor("15min")
        rows = self.postgres.fetch_all(
            """SELECT timestamp,open,high,low,close
                 FROM market_data.ohlc
                WHERE LOWER(TRIM(exchange))=LOWER(TRIM(%s))
                  AND pair=%s AND timeframe='15m'
                  AND timestamp >= %s - INTERVAL '60 hours'
                  AND timestamp <= %s + INTERVAL '75 minutes'
                ORDER BY timestamp ASC""",
            (trade["exchange"], trade["pair"], ref.to_pydatetime(), ref.to_pydatetime()),
        )
        frame = pd.DataFrame(rows, columns=["timestamp","open","high","low","close"])
        if frame.empty:
            return frame
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        for col in ["open","high","low","close"]:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame["ema20"] = frame["close"].ewm(span=20, adjust=False).mean()
        frame["ema200"] = frame["close"].ewm(span=200, adjust=False).mean()
        return frame.dropna().reset_index(drop=True)

    def _metrics(self, trade: dict[str, Any], frame: pd.DataFrame) -> dict[str, float] | None:
        if frame.empty:
            return None
        ref = self._utc(trade["reference"]).floor("15min")
        matches = frame.index[frame["timestamp"] == ref].tolist()
        if not matches:
            return None
        i = matches[-1]
        if i < 5:
            return None
        trigger = frame.iloc[i]
        prev = frame.iloc[i-1]
        pre2 = frame.iloc[i-2]
        pre3 = frame.iloc[i-3]
        direction = str(trade["state"].get("direction","LONG")).upper()
        entry, stop = trade["entry"], trade["stop"]
        risk = abs(entry-stop)
        if risk <= 0:
            return None

        body = abs(float(trigger["close"]-trigger["open"]))
        range_ = max(float(trigger["high"]-trigger["low"]), 1e-12)
        prev_ranges = [max(float(frame.iloc[j]["high"]-frame.iloc[j]["low"]),1e-12) for j in range(max(0,i-5),i)]
        avg_prev_range = sum(prev_ranges)/len(prev_ranges)
        slope20_2 = (float(trigger["ema20"])/float(pre2["ema20"])-1.0)
        slope20_4 = (float(trigger["ema20"])/float(frame.iloc[i-4]["ema20"])-1.0) if i >= 4 else slope20_2
        sep = abs(float(trigger["ema20"])/float(trigger["ema200"])-1.0)
        extension_r = abs(float(trigger["close"])-float(trigger["ema20"]))/risk
        extension_pct = abs(float(trigger["close"])/float(trigger["ema20"])-1.0)
        body_ratio = body/range_
        range_expansion = range_/avg_prev_range if avg_prev_range > 0 else 0.0

        pb = frame.iloc[max(0,i-3):i]
        if direction == "LONG":
            pullback_depth_r = max(0.0,(float(pb["high"].max())-float(pb["low"].min()))/risk)
            trigger_break_r = max(0.0,(float(trigger["close"])-float(prev["high"]))/risk)
        else:
            pullback_depth_r = max(0.0,(float(pb["high"].max())-float(pb["low"].min()))/risk)
            trigger_break_r = max(0.0,(float(prev["low"])-float(trigger["close"]))/risk)

        post = frame.iloc[i+1:i+5]
        post_mfe = 0.0
        post_mae = 0.0
        if not post.empty:
            if direction == "LONG":
                post_mfe = max(0.0,(float(post["high"].max())-entry)/risk)
                post_mae = max(0.0,(entry-float(post["low"].min()))/risk)
            else:
                post_mfe = max(0.0,(entry-float(post["low"].min()))/risk)
                post_mae = max(0.0,(float(post["high"].max())-entry)/risk)
        first_close_r = 0.0
        if len(post) >= 1:
            c = float(post.iloc[0]["close"])
            first_close_r = ((c-entry)/risk) if direction == "LONG" else ((entry-c)/risk)

        return {
            "ema20_slope_2": slope20_2,
            "ema20_slope_4": slope20_4,
            "ema20_ema200_sep": sep,
            "extension_r": extension_r,
            "extension_pct": extension_pct,
            "trigger_body_ratio": body_ratio,
            "trigger_range_expansion": range_expansion,
            "trigger_break_r": trigger_break_r,
            "pullback_depth_r": pullback_depth_r,
            "post4_mfe_r": post_mfe,
            "post4_mae_r": post_mae,
            "first_post_close_r": first_close_r,
        }

    @staticmethod
    def _avg(rows: list[dict[str, Any]], key: str) -> float:
        vals = [float(r[key]) for r in rows if key in r]
        return sum(vals)/len(vals) if vals else 0.0

    def run(self) -> dict[str, Any]:
        self.logger.info("VELEZ_ENTRY_AUDIT_START")
        try:
            trades = self._trades()
        except Exception:
            self.logger.exception("VELEZ_ENTRY_AUDIT_QUERY_FAILED")
            return {"trades":0}
        analysed: list[dict[str, Any]] = []
        for trade in trades:
            try:
                metrics = self._metrics(trade, self._context(trade))
                if metrics is None:
                    self.logger.warning("VELEZ_ENTRY_AUDIT_NO_CONTEXT id=%s pair=%s",trade["id"],trade["pair"])
                    continue
                row = {**trade, **metrics}
                analysed.append(row)
                self.logger.info(
                    "VELEZ_ENTRY_AUDIT_TRADE id=%s pair=%s net=%.4f ext_r=%.3f body=%.3f range_exp=%.3f break_r=%.3f slope2=%.6f sep=%.6f post4_mfe=%.3f post4_mae=%.3f first_close=%.3f",
                    trade["id"], trade["pair"], trade["net"], metrics["extension_r"],
                    metrics["trigger_body_ratio"], metrics["trigger_range_expansion"], metrics["trigger_break_r"],
                    metrics["ema20_slope_2"], metrics["ema20_ema200_sep"], metrics["post4_mfe_r"],
                    metrics["post4_mae_r"], metrics["first_post_close_r"],
                )
            except Exception:
                self.logger.exception("VELEZ_ENTRY_AUDIT_TRADE_FAILED id=%s",trade.get("id"))
        if not analysed:
            return {"trades":0}
        losers = [r for r in analysed if r["net"] < 0]
        winners = [r for r in analysed if r["net"] > 0]
        weak_follow = [r for r in analysed if r["post4_mfe_r"] < 0.50]
        summary = {
            "trades": len(analysed),
            "winners": len(winners),
            "losers": len(losers),
            "weak_followthrough": len(weak_follow),
            "loser_extension_avg_r": self._avg(losers,"extension_r"),
            "loser_body_ratio_avg": self._avg(losers,"trigger_body_ratio"),
            "loser_range_expansion_avg": self._avg(losers,"trigger_range_expansion"),
            "loser_break_avg_r": self._avg(losers,"trigger_break_r"),
            "loser_slope2_avg": self._avg(losers,"ema20_slope_2"),
            "loser_sep_avg": self._avg(losers,"ema20_ema200_sep"),
            "loser_post4_mfe_avg_r": self._avg(losers,"post4_mfe_r"),
            "loser_post4_mae_avg_r": self._avg(losers,"post4_mae_r"),
            "loser_first_close_avg_r": self._avg(losers,"first_post_close_r"),
            "extended_over_0_75r": sum(1 for r in analysed if r["extension_r"] >= 0.75),
            "trigger_range_over_1_5x": sum(1 for r in analysed if r["trigger_range_expansion"] >= 1.5),
            "first_close_negative": sum(1 for r in analysed if r["first_post_close_r"] < 0),
        }
        self.logger.warning("VELEZ_ENTRY_AUDIT_SUMMARY %s",summary)
        return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        if int(s.get("trades",0)) == 0:
            return "📊 <b>AUDIT INGRESSI VELEZ 15m</b>\nNessun trade con contesto OHLC sufficiente."
        return (
            "📊 <b>AUDIT INGRESSI VELEZ 15m</b>\n"
            f"Trade analizzati: <b>{int(s['trades'])}</b>\n"
            f"🟢 Vincenti: <b>{int(s['winners'])}</b> · 🔴 Perdenti: <b>{int(s['losers'])}</b>\n"
            f"Follow-through debole (&lt; +0,50R nelle prime 4 candele): <b>{int(s['weak_followthrough'])}</b>\n\n"
            "<b>Media sui trade perdenti</b>\n"
            f"Estensione ingresso da EMA20: <b>{s['loser_extension_avg_r']:.2f}R</b>\n"
            f"Corpo candela trigger / range: <b>{s['loser_body_ratio_avg']:.2f}</b>\n"
            f"Range trigger vs 5 barre precedenti: <b>{s['loser_range_expansion_avg']:.2f}x</b>\n"
            f"Break oltre barra precedente: <b>{s['loser_break_avg_r']:.2f}R</b>\n"
            f"MFE prime 4 candele: <b>+{s['loser_post4_mfe_avg_r']:.2f}R</b>\n"
            f"MAE prime 4 candele: <b>-{s['loser_post4_mae_avg_r']:.2f}R</b>\n"
            f"Chiusura prima candela post-entry: <b>{s['loser_first_close_avg_r']:+.2f}R</b>\n\n"
            "<b>Possibili segnali di ingresso tardivo/debole</b>\n"
            f"Ingresso già ≥0,75R dalla EMA20: <b>{int(s['extended_over_0_75r'])}</b>\n"
            f"Candela trigger ≥1,5x range recente: <b>{int(s['trigger_range_over_1_5x'])}</b>\n"
            f"Prima candela successiva chiude negativa in R: <b>{int(s['first_close_negative'])}</b>\n\n"
            "⚠️ Audit diagnostico: nessun filtro live è stato modificato."
        )
