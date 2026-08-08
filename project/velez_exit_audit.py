"""Diagnostic audit for Velez 15m exits. Does not modify live trading rules."""
from __future__ import annotations

import json
from typing import Any

import pandas as pd


class VelezExitAudit:
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
        out = []
        for r in rows:
            out.append({
                "id": int(r[0]), "pair": str(r[1]), "entry": float(r[2]), "stop": float(r[3]),
                "reference": r[4] or r[6] or r[5], "created": r[5], "sent": r[6],
                "closed": r[7], "exchange": str(r[8]), "state": self._state(r[9]),
                "net": float(r[10] or 0) - float(r[11] or 0), "status": str(r[12]),
                "resolution": str(r[13]),
            })
        return out

    def _candles(self, trade: dict[str, Any]) -> pd.DataFrame:
        start = pd.Timestamp(trade["reference"])
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        end = pd.Timestamp(trade["closed"])
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
        rows = self.postgres.fetch_all(
            """SELECT timestamp,open,high,low,close
                 FROM market_data.ohlc
                WHERE LOWER(TRIM(exchange))=LOWER(TRIM(%s))
                  AND pair=%s AND timeframe='15m'
                  AND timestamp>%s AND timestamp<=%s
                ORDER BY timestamp ASC""",
            (trade["exchange"], trade["pair"], start.floor("15min").to_pydatetime(), end.to_pydatetime()),
        )
        return pd.DataFrame(rows, columns=["timestamp","open","high","low","close"])

    @staticmethod
    def _mfe_mae_r(trade: dict[str, Any], frame: pd.DataFrame) -> tuple[float,float]:
        if frame.empty:
            return 0.0, 0.0
        entry, stop = trade["entry"], trade["stop"]
        risk = abs(entry-stop)
        if risk <= 0:
            return 0.0, 0.0
        direction = str(trade["state"].get("direction","LONG")).upper()
        if direction == "LONG":
            mfe = (float(frame["high"].max())-entry)/risk
            mae = (entry-float(frame["low"].min()))/risk
        else:
            mfe = (entry-float(frame["low"].min()))/risk
            mae = (float(frame["high"].max())-entry)/risk
        return float(mfe), float(mae)

    @staticmethod
    def _simulate_lock(trade: dict[str, Any], frame: pd.DataFrame, trigger_r: float, lock_r: float) -> float:
        """Return outcome in R. Trigger candle arms protection for following candle only."""
        entry, stop = trade["entry"], trade["stop"]
        risk = abs(entry-stop)
        if risk <= 0 or frame.empty:
            return 0.0
        direction = str(trade["state"].get("direction","LONG")).upper()
        armed = False
        active_stop = stop
        for _, row in frame.iterrows():
            high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
            stop_hit = low <= active_stop if direction == "LONG" else high >= active_stop
            if stop_hit:
                return ((active_stop-entry)/risk) if direction == "LONG" else ((entry-active_stop)/risk)
            trigger_price = entry + trigger_r*risk if direction == "LONG" else entry-trigger_r*risk
            if not armed:
                hit = high >= trigger_price if direction == "LONG" else low <= trigger_price
                if hit:
                    armed = True
                    active_stop = entry + lock_r*risk if direction == "LONG" else entry-lock_r*risk
        last = float(frame.iloc[-1]["close"])
        return ((last-entry)/risk) if direction == "LONG" else ((entry-last)/risk)

    def run(self) -> dict[str, Any]:
        self.logger.info("VELEZ_EXIT_AUDIT_START")
        try:
            trades = self._trades()
        except Exception:
            self.logger.exception("VELEZ_EXIT_AUDIT_QUERY_FAILED")
            return {"trades": 0}
        rows = []
        rules = [(0.50,0.00),(0.75,0.25),(1.00,0.50),(1.50,1.00)]
        for trade in trades:
            try:
                frame = self._candles(trade)
                mfe, mae = self._mfe_mae_r(trade, frame)
                sims = {f"t{t:.2f}_l{l:.2f}": self._simulate_lock(trade, frame, t, l) for t,l in rules}
                rows.append({**trade,"mfe":mfe,"mae":mae,**sims})
                self.logger.info("VELEZ_EXIT_AUDIT_TRADE id=%s pair=%s net=%.4f mfe_r=%.3f mae_r=%.3f sims=%s", trade["id"],trade["pair"],trade["net"],mfe,mae,sims)
            except Exception:
                self.logger.exception("VELEZ_EXIT_AUDIT_TRADE_FAILED id=%s", trade.get("id"))
        if not rows:
            self.logger.info("VELEZ_EXIT_AUDIT_DONE trades=0")
            return {"trades":0}
        losers = [r for r in rows if r["net"] < 0]
        winners = [r for r in rows if r["net"] > 0]
        thresholds = {x: sum(1 for r in losers if r["mfe"] >= x) for x in (0.25,0.50,0.75,1.00,1.50,2.00)}
        sim_avg = {f"t{t:.2f}_l{l:.2f}": sum(r[f"t{t:.2f}_l{l:.2f}"] for r in rows)/len(rows) for t,l in rules}
        best = max(sim_avg, key=sim_avg.get)
        summary = {
            "trades": len(rows), "winners": len(winners), "losers": len(losers),
            "actual_net_eur": sum(r["net"] for r in rows),
            "loser_mfe_avg_r": (sum(r["mfe"] for r in losers)/len(losers)) if losers else 0.0,
            "loser_mae_avg_r": (sum(r["mae"] for r in losers)/len(losers)) if losers else 0.0,
            "loser_threshold_counts": thresholds,
            "simulated_avg_r": sim_avg, "best_rule": best,
        }
        self.logger.warning("VELEZ_EXIT_AUDIT_SUMMARY %s", summary)
        return summary
