"""One-shot historical candidate audit for the Velez 15m setup.

Diagnostic only. Replays the setup on stored closed 15m candles, including near-candidates
rejected by individual Velez rules, and measures whether +1R or -1R is touched first over
4/8/12/24 bars. A DB version marker prevents expensive reruns after every Railway restart.
"""
from __future__ import annotations

from typing import Any
import math
import statistics

import pandas as pd

AUDIT_VERSION = "VELEZ_CANDIDATE_EDGE_V1_20260808"


class VelezCandidateAudit:
    HORIZONS = (4, 8, 12, 24)

    def __init__(self, postgres: Any, logger: Any, pairs: list[str], exchange: str = "kraken", pullback_bars: int = 3, max_bars_per_pair: int = 3200) -> None:
        self.postgres = postgres
        self.logger = logger
        self.pairs = list(pairs)
        self.exchange = str(exchange)
        self.pullback_bars = max(2, int(pullback_bars))
        self.max_bars_per_pair = max(500, int(max_bars_per_pair))

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
        import json
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
                      AND pair=%s AND timeframe='15m'
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
        d["range"] = (d["high"] - d["low"]).abs()
        d["range_avg5"] = d["range"].rolling(5).mean()
        return d

    @staticmethod
    def _first_touch(direction: str, entry: float, risk: float, future: pd.DataFrame, horizon: int) -> str:
        if risk <= 0:
            return "INVALID"
        tp = entry + risk if direction == "LONG" else entry - risk
        sl = entry - risk if direction == "LONG" else entry + risk
        for _, r in future.head(horizon).iterrows():
            hi, lo = float(r["high"]), float(r["low"])
            if direction == "LONG":
                hit_tp, hit_sl = hi >= tp, lo <= sl
            else:
                hit_tp, hit_sl = lo <= tp, hi >= sl
            if hit_tp and hit_sl:
                return "AMBIGUOUS"
            if hit_sl:
                return "SL"
            if hit_tp:
                return "TP"
        return "NONE"

    def _candidate_rows(self, pair: str) -> list[dict[str, Any]]:
        d = self._frame(pair)
        out: list[dict[str, Any]] = []
        if len(d) < 230:
            return out
        start = max(205, self.pullback_bars + 3)
        end = len(d) - max(self.HORIZONS)
        for i in range(start, end):
            latest, prev = d.iloc[i], d.iloc[i-1]
            pull = d.iloc[i-self.pullback_bars:i]
            ema20_now, ema20_2 = float(latest["ema20"]), float(d.iloc[i-2]["ema20"])
            close, ema200 = float(latest["close"]), float(latest["ema200"])
            long_trend = close > ema20_now > ema200 and ema20_now > ema20_2
            short_trend = close < ema20_now < ema200 and ema20_now < ema20_2
            if not long_trend and not short_trend:
                continue
            direction = "LONG" if long_trend else "SHORT"
            if direction == "LONG":
                ordered = all(float(pull.iloc[j]["high"]) >= float(pull.iloc[j+1]["high"]) for j in range(len(pull)-1))
                touched20 = bool((pull["low"] <= pull["ema20"]).any())
                held200 = bool((pull["close"] > pull["ema200"]).all())
                trigger = bool(float(latest["close"]) > float(latest["open"]) and float(latest["high"]) > float(prev["high"]) and float(latest["close"]) > float(prev["high"]) and close > ema20_now)
                stop = float(pull["low"].min()) - close*0.0005
                break_abs = max(0.0, close-float(prev["high"]))
            else:
                ordered = all(float(pull.iloc[j]["low"]) <= float(pull.iloc[j+1]["low"]) for j in range(len(pull)-1))
                touched20 = bool((pull["high"] >= pull["ema20"]).any())
                held200 = bool((pull["close"] < pull["ema200"]).all())
                trigger = bool(float(latest["close"]) < float(latest["open"]) and float(latest["low"]) < float(prev["low"]) and float(latest["close"]) < float(prev["low"]) and close < ema20_now)
                stop = float(pull["high"].max()) + close*0.0005
                break_abs = max(0.0, float(prev["low"])-close)
            risk = abs(close-stop)
            if risk <= 0 or risk/close > 0.03:
                continue

            if not ordered:
                stage = "PULLBACK_NOT_ORDERED"
            elif not touched20:
                stage = "NO_PULLBACK_TO_EMA20"
            elif not held200:
                stage = "EMA200_STRUCTURE_FAILED"
            elif not trigger:
                stage = "NO_PRIOR_BAR_BREAK_TRIGGER"
            else:
                stage = "QUALIFIED_TECHNICAL"

            candle_range = max(float(latest["high"])-float(latest["low"]), 1e-12)
            body_ratio = abs(float(latest["close"])-float(latest["open"]))/candle_range
            range_avg5 = float(latest["range_avg5"]) if not pd.isna(latest["range_avg5"]) else candle_range
            future = d.iloc[i+1:i+1+max(self.HORIZONS)]
            row: dict[str, Any] = {
                "pair": pair, "time": latest["timestamp"], "direction": direction, "stage": stage,
                "ordered": ordered, "touched20": touched20, "held200": held200, "trigger": trigger,
                "risk_pct": 100.0*risk/close,
                "ema20_slope_bp": 10000.0*abs(ema20_now/ema20_2-1.0) if ema20_2 else 0.0,
                "ema_sep_pct": 100.0*abs(ema20_now/ema200-1.0) if ema200 else 0.0,
                "entry_ema20_r": abs(close-ema20_now)/risk,
                "break_r": break_abs/risk,
                "body_ratio": body_ratio,
                "range_vs5": candle_range/range_avg5 if range_avg5 > 0 else 0.0,
                "utc_hour": int(pd.Timestamp(latest["timestamp"]).hour),
            }
            for h in self.HORIZONS:
                row[f"h{h}"] = self._first_touch(direction, close, risk, future, h)
            out.append(row)
        return out

    @staticmethod
    def _rate(rows: list[dict[str, Any]], horizon: int) -> dict[str, float]:
        resolved = [r for r in rows if r[f"h{horizon}"] in {"TP","SL"}]
        tp = sum(1 for r in resolved if r[f"h{horizon}"] == "TP")
        sl = len(resolved)-tp
        return {"n": len(rows), "resolved": len(resolved), "tp": tp, "sl": sl, "tp_rate": 100.0*tp/len(resolved) if resolved else 0.0}

    @staticmethod
    def _feature_medians(rows: list[dict[str, Any]], horizon: int = 24) -> dict[str, dict[str, float]]:
        tp = [r for r in rows if r[f"h{horizon}"] == "TP"]
        sl = [r for r in rows if r[f"h{horizon}"] == "SL"]
        features = ("risk_pct","ema20_slope_bp","ema_sep_pct","entry_ema20_r","break_r","body_ratio","range_vs5")
        out: dict[str, dict[str, float]] = {}
        for f in features:
            out[f] = {
                "tp": statistics.median([float(r[f]) for r in tp]) if tp else 0.0,
                "sl": statistics.median([float(r[f]) for r in sl]) if sl else 0.0,
            }
        return out

    def run(self) -> dict[str, Any]:
        if self.already_done():
            return {"skipped": True, "version": AUDIT_VERSION}
        self.logger.info("VELEZ_CANDIDATE_AUDIT_START version=%s pairs=%s max_bars=%s", AUDIT_VERSION, len(self.pairs), self.max_bars_per_pair)
        rows: list[dict[str, Any]] = []
        for pair in self.pairs:
            try:
                part = self._candidate_rows(pair)
                rows.extend(part)
                self.logger.info("VELEZ_CANDIDATE_AUDIT_PAIR pair=%s candidates=%s", pair, len(part))
            except Exception:
                self.logger.exception("VELEZ_CANDIDATE_AUDIT_PAIR_FAILED pair=%s", pair)

        stages = sorted({r["stage"] for r in rows})
        stage_stats = {stage: {f"h{h}": self._rate([r for r in rows if r["stage"] == stage], h) for h in self.HORIZONS} for stage in stages}
        qualified = [r for r in rows if r["stage"] == "QUALIFIED_TECHNICAL"]
        all_stats = {f"h{h}": self._rate(rows, h) for h in self.HORIZONS}
        q_stats = {f"h{h}": self._rate(qualified, h) for h in self.HORIZONS}
        features = self._feature_medians(qualified or rows, 24)
        summary = {
            "version": AUDIT_VERSION, "candidates": len(rows), "qualified": len(qualified),
            "all": all_stats, "qualified_stats": q_stats, "stages": stage_stats, "features": features,
        }
        self._save_marker(summary)
        self.logger.warning("VELEZ_CANDIDATE_AUDIT_DONE version=%s candidates=%s qualified=%s", AUDIT_VERSION, len(rows), len(qualified))
        return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        if s.get("skipped"):
            return ""
        lines = [
            "🧪 <b>AUDIT VELEZ · CANDIDATI STORICI</b>",
            f"Candidati con trend EMA valido: <b>{s.get('candidates',0)}</b>",
            f"Setup tecnicamente qualificati: <b>{s.get('qualified',0)}</b>",
            "",
            "🎯 <b>+1R prima di -1R · setup qualificati</b>",
        ]
        for h in (4,8,12,24):
            x = s.get("qualified_stats",{}).get(f"h{h}",{})
            lines.append(f"• entro {h} barre: risolti <b>{x.get('resolved',0)}</b> · successo <b>{x.get('tp_rate',0):.1f}%</b>")
        lines += ["", "🚦 <b>Esito a 24 barre per stadio</b>"]
        ranking = []
        for stage, stats in s.get("stages",{}).items():
            x = stats.get("h24",{})
            if x.get("resolved",0) >= 10:
                ranking.append((x.get("tp_rate",0), stage, x))
        for rate, stage, x in sorted(ranking, reverse=True)[:6]:
            lines.append(f"• {stage}: n=<b>{x.get('n',0)}</b> · risolti <b>{x.get('resolved',0)}</b> · +1R-first <b>{rate:.1f}%</b>")
        lines += ["", "🔬 <b>Mediane winner vs loser · qualificati · 24 barre</b>"]
        labels = {
            "risk_pct":"stop %", "ema20_slope_bp":"pendenza EMA20 bp", "ema_sep_pct":"separazione EMA20/200 %",
            "entry_ema20_r":"distanza entry-EMA20 R", "break_r":"forza rottura R", "body_ratio":"corpo/range", "range_vs5":"range vs 5 barre",
        }
        for key, vals in s.get("features",{}).items():
            lines.append(f"• {labels.get(key,key)}: winner <b>{vals.get('tp',0):.3f}</b> · loser <b>{vals.get('sl',0):.3f}</b>")
        lines.append("\n⚠️ Diagnostico storico, nessuna modifica al live. Doppio tocco +1R/-1R nella stessa candela resta AMBIGUO ed è escluso dal tasso di successo. L'audit viene eseguito una sola volta per versione per limitare il carico Railway.")
        return "\n".join(lines)
