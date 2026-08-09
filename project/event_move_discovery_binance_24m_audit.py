"""24-month event-first crypto edge discovery on Binance Spot.

This audit deliberately does NOT start from an entry/exit strategy.  It asks a
more basic question: after a predeclared market event, is there a directional
move large enough to survive realistic costs?

Protocol (fixed before results):
- BTC/USDT, ETH/USDT, SOL/USDT; Binance Spot public 15m klines.
- Fetch 2024-07-01 -> 2026-08-09 UTC.
- Research year A: 2024-08-09 -> 2025-08-09.
  DEV 6m, VALIDATION 3m, HOLDOUT 3m.
- External replication year B: 2025-08-09 -> 2026-08-09.
- Timeframes: 15m, 30m, 1h, 2h, 4h.
- Forward horizons: 4h, 8h, 12h.
- 18 predeclared directional event definitions x 5 TF x 3 horizons = 270
  pooled candidates. Pair-specific diagnostics are reported but never used to
  promote a candidate, limiting multiple-testing pressure.
- Entry benchmark: next-bar open after the event is known on a closed bar.
- Exit benchmark: close at the fixed forward horizon. No stop/TP optimization.
- Non-overlapping observations per pair/candidate/horizon.
- Costs: configured taker fee each side + configured spread/slippage.

An event is only promoted from one phase to the next if its average gross and
net directional drift remain positive with a minimum sample. The external
replication year is never used for candidate selection.
"""
from __future__ import annotations

from typing import Any
import json
import math
import random
import statistics

import pandas as pd
import requests

from project.sma20_engulfing_binance_12m_audit import Sma20EngulfingBinance12mAudit
from project.cryptoresearch_v1_cand000006_binance_12m_v2_audit import ARCHIVE_BASE
from project.cryptoresearch_v1_cand000006_binance_12m_audit import (
    SYMBOLS,
    PAIR_NAMES,
    INTERVAL,
)

AUDIT_VERSION = "EVENT_MOVE_DISCOVERY_BINANCE_24M_20260809_V1"
FETCH_START = pd.Timestamp("2024-07-01T00:00:00Z")
DEV_START = pd.Timestamp("2024-08-09T00:00:00Z")
DEV_END = pd.Timestamp("2025-02-09T00:00:00Z")
VAL_END = pd.Timestamp("2025-05-09T00:00:00Z")
HOLDOUT_END = pd.Timestamp("2025-08-09T00:00:00Z")
REPLICATION_END = pd.Timestamp("2026-08-09T00:00:00Z")

TIMEFRAMES: dict[str, str] = {
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
}
TF_MINUTES = {"15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240}
HORIZONS_H = (4, 8, 12)

MIN_DEV = 30
MIN_VAL = 15
MIN_HOLDOUT = 15
MIN_REPLICATION = 30
PROMOTE_GROSS = 0.0050   # 0.50% average directional move before costs
PROMOTE_NET = 0.0010     # at least +0.10% after modeled costs
REPL_GROSS = 0.0050
REPL_NET = 0.0010


class EventMoveDiscoveryBinance24mAudit(Sma20EngulfingBinance12mAudit):
    def already_done(self) -> bool:
        self._ensure_marker()
        return bool(self.postgres.fetch_all(
            "SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1",
            (AUDIT_VERSION,),
        ))

    def _save_marker(self, summary: dict[str, Any]) -> None:
        self.postgres.execute(
            "INSERT INTO statistics.velez_candidate_audit_runs(version,summary) VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING",
            (AUDIT_VERSION, json.dumps(summary, default=str)),
        )

    @staticmethod
    def _archive_urls(symbol: str) -> list[str]:
        urls: list[str] = []
        for period in pd.period_range("2024-07", "2026-07", freq="M"):
            ym = period.strftime("%Y-%m")
            urls.append(
                f"{ARCHIVE_BASE}/monthly/klines/{symbol}/{INTERVAL}/{symbol}-{INTERVAL}-{ym}.zip"
            )
        for day in pd.date_range("2026-08-01", "2026-08-08", freq="D", tz="UTC"):
            ymd = day.strftime("%Y-%m-%d")
            urls.append(
                f"{ARCHIVE_BASE}/daily/klines/{symbol}/{INTERVAL}/{symbol}-{INTERVAL}-{ymd}.zip"
            )
        return urls

    def _fetch_symbol(self, symbol: str) -> tuple[pd.DataFrame, dict[str, Any]]:
        frames: list[pd.DataFrame] = []
        session = requests.Session()
        session.headers.update({"User-Agent": "event-move-discovery/1.0"})
        urls = self._archive_urls(symbol)
        for url in urls:
            response = session.get(url, timeout=30)
            if response.status_code != 200:
                raise RuntimeError(
                    f"BINANCE_ARCHIVE_HTTP symbol={symbol} status={response.status_code} file={url.rsplit('/',1)[-1]}"
                )
            frames.append(self._read_zip(response.content))
        frame = pd.concat(frames, ignore_index=True)
        frame = frame[(frame["timestamp"] >= FETCH_START) & (frame["timestamp"] < REPLICATION_END)]
        frame = frame.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
        expected_min = 70000
        if len(frame) < expected_min:
            raise RuntimeError(f"BINANCE_24M_HISTORY_TOO_SHORT symbol={symbol} bars={len(frame)}")
        diffs = frame["timestamp"].diff().dropna().dt.total_seconds().div(900.0)
        missing = int(sum(max(0, int(round(v)) - 1) for v in diffs if v > 1.0))
        return frame, {
            "symbol": symbol,
            "pair": PAIR_NAMES[symbol],
            "bars": len(frame),
            "from": str(frame.iloc[0]["timestamp"]),
            "to": str(frame.iloc[-1]["timestamp"]),
            "requests": len(urls),
            "missing_intervals": missing,
            "transport": "BINANCE_VISION_ARCHIVES",
        }

    @staticmethod
    def _phase(ts: pd.Timestamp) -> str | None:
        if ts < DEV_START or ts >= REPLICATION_END:
            return None
        if ts < DEV_END:
            return "DEV"
        if ts < VAL_END:
            return "VAL"
        if ts < HOLDOUT_END:
            return "HOLDOUT"
        return "REPLICATION"

    @staticmethod
    def _resample24(raw: pd.DataFrame, tf: str) -> pd.DataFrame:
        if tf == "15m":
            x = raw[["timestamp", "open", "high", "low", "close", "volume"]].copy()
            return x.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
        rule = TIMEFRAMES[tf]
        x = raw.set_index("timestamp").resample(
            rule, origin="epoch", label="left", closed="left"
        ).agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna().reset_index()
        mins = TF_MINUTES[tf]
        return x[(x["timestamp"] + pd.Timedelta(minutes=mins)) <= REPLICATION_END].reset_index(drop=True)

    @staticmethod
    def _prepare_events(raw: pd.DataFrame, tf: str) -> tuple[pd.DataFrame, dict[str, tuple[pd.Series, str]]]:
        x = EventMoveDiscoveryBinance24mAudit._resample24(raw, tf)
        op = x["open"].astype(float)
        hi = x["high"].astype(float)
        lo = x["low"].astype(float)
        cl = x["close"].astype(float)
        vol = x["volume"].astype(float)

        prev_close = cl.shift(1)
        tr = pd.concat([
            hi - lo,
            (hi - prev_close).abs(),
            (lo - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr20 = tr.rolling(20).mean()
        rvol20 = vol / vol.rolling(20).mean().replace(0.0, math.nan)
        rng = (hi - lo).replace(0.0, math.nan)
        close_loc = (cl - lo) / rng

        prev_high20 = hi.rolling(20).max().shift(1)
        prev_low20 = lo.rolling(20).min().shift(1)
        prev_high50 = hi.rolling(50).max().shift(1)
        prev_low50 = lo.rolling(50).min().shift(1)

        mom4_bars = max(1, int(round(240 / TF_MINUTES[tf])))
        mom4h = cl / cl.shift(mom4_bars) - 1.0
        ret1 = cl / prev_close - 1.0

        channel_width = (prev_high20 - prev_low20) / prev_close.replace(0.0, math.nan)
        compression_q25 = channel_width.rolling(100).quantile(0.25).shift(1)
        compressed = channel_width <= compression_q25

        range_exp = tr / atr20.replace(0.0, math.nan) >= 1.5
        vol_burst = rvol20 >= 2.5
        range_vol = range_exp & (rvol20 >= 2.0)

        events: dict[str, tuple[pd.Series, str]] = {
            "BREAK20_UP": (cl > prev_high20, "LONG"),
            "BREAK20_DOWN": (cl < prev_low20, "SHORT"),
            "BREAK50_UP": (cl > prev_high50, "LONG"),
            "BREAK50_DOWN": (cl < prev_low50, "SHORT"),
            "MOM4H_1P5_UP": (mom4h >= 0.015, "LONG"),
            "MOM4H_1P5_DOWN": (mom4h <= -0.015, "SHORT"),
            "MOM4H_3P0_UP": (mom4h >= 0.030, "LONG"),
            "MOM4H_3P0_DOWN": (mom4h <= -0.030, "SHORT"),
            "RANGE_EXP_UP": (range_exp & (cl > op) & (close_loc >= 0.80), "LONG"),
            "RANGE_EXP_DOWN": (range_exp & (cl < op) & (close_loc <= 0.20), "SHORT"),
            "RANGE_VOL_UP": (range_vol & (cl > op) & (close_loc >= 0.80), "LONG"),
            "RANGE_VOL_DOWN": (range_vol & (cl < op) & (close_loc <= 0.20), "SHORT"),
            "VOL_BURST_UP": (vol_burst & (cl > op) & (close_loc >= 0.65), "LONG"),
            "VOL_BURST_DOWN": (vol_burst & (cl < op) & (close_loc <= 0.35), "SHORT"),
            "COMP_BREAK_UP": (compressed & (cl > prev_high20), "LONG"),
            "COMP_BREAK_DOWN": (compressed & (cl < prev_low20), "SHORT"),
            "SHOCK_REVERT_POS": (ret1 >= 0.015, "SHORT"),
            "SHOCK_REVERT_NEG": (ret1 <= -0.015, "LONG"),
        }
        return x, events

    def _observation(
        self,
        x: pd.DataFrame,
        pair: str,
        tf: str,
        event_name: str,
        direction: str,
        signal_i: int,
        horizon_h: int,
    ) -> dict[str, Any] | None:
        entry_i = signal_i + 1
        hbars = max(1, int(math.ceil(horizon_h * 60 / TF_MINUTES[tf])))
        end_i = entry_i + hbars - 1
        if entry_i >= len(x) or end_i >= len(x):
            return None
        entry_time = pd.Timestamp(x.iloc[entry_i]["timestamp"])
        if entry_time < DEV_START or entry_time >= REPLICATION_END:
            return None
        end_time = pd.Timestamp(x.iloc[end_i]["timestamp"])
        if end_time >= REPLICATION_END:
            return None
        entry = float(x.iloc[entry_i]["open"])
        exit_px = float(x.iloc[end_i]["close"])
        if not math.isfinite(entry) or not math.isfinite(exit_px) or entry <= 0 or exit_px <= 0:
            return None
        path = x.iloc[entry_i:end_i + 1]
        if direction == "LONG":
            mfe = float(path["high"].max()) / entry - 1.0
            mae = 1.0 - float(path["low"].min()) / entry
        else:
            mfe = 1.0 - float(path["low"].min()) / entry
            mae = float(path["high"].max()) / entry - 1.0
        gross, fee_only, net = self._net_return(entry, exit_px, direction)
        return {
            "pair": pair,
            "tf": tf,
            "event": event_name,
            "direction": direction,
            "horizon_h": horizon_h,
            "entry_time": entry_time,
            "exit_time": end_time,
            "phase": self._phase(entry_time),
            "gross_r": gross,
            "fee_r": fee_only,
            "net_r": net,
            "mfe": max(0.0, mfe),
            "mae": max(0.0, mae),
        }

    def _collect_candidate(
        self,
        prepared: dict[tuple[str, str], tuple[pd.DataFrame, dict[str, tuple[pd.Series, str]]]],
        tf: str,
        event_name: str,
        horizon_h: int,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        hbars = max(1, int(math.ceil(horizon_h * 60 / TF_MINUTES[tf])))
        for pair in PAIR_NAMES.values():
            x, events = prepared[(pair, tf)]
            mask, direction = events[event_name]
            indices = [int(i) for i in x.index[mask.fillna(False)]]
            next_allowed = -1
            for i in indices:
                entry_i = i + 1
                if entry_i <= next_allowed:
                    continue
                obs = self._observation(x, pair, tf, event_name, direction, i, horizon_h)
                if obs is None:
                    continue
                out.append(obs)
                next_allowed = entry_i + hbars - 1
        return out

    @staticmethod
    def _bootstrap(values: list[float], seed: int, nboot: int = 3000) -> tuple[float, float]:
        if len(values) < 10:
            return (0.0, 0.0)
        rng = random.Random(seed)
        n = len(values)
        means: list[float] = []
        for _ in range(nboot):
            means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
        means.sort()
        return means[int(0.025 * nboot)], means[min(nboot - 1, int(0.975 * nboot))]

    @classmethod
    def _stats(cls, rows: list[dict[str, Any]], seed: int = 1) -> dict[str, Any]:
        gross = [float(r["gross_r"]) for r in rows]
        net = [float(r["net_r"]) for r in rows]
        mfe = [float(r["mfe"]) for r in rows]
        mae = [float(r["mae"]) for r in rows]
        pair_net: dict[str, float] = {}
        for r in rows:
            pair_net[r["pair"]] = pair_net.get(r["pair"], 0.0) + float(r["net_r"])
        positive_total = sum(v for v in pair_net.values() if v > 0)
        top_positive = max([v for v in pair_net.values() if v > 0], default=0.0)
        ci = cls._bootstrap(net, seed)
        return {
            "n": len(rows),
            "gross_exp": statistics.mean(gross) if gross else 0.0,
            "net_exp": statistics.mean(net) if net else 0.0,
            "median_net": statistics.median(net) if net else 0.0,
            "positive_rate": 100.0 * sum(1 for v in net if v > 0) / len(net) if net else 0.0,
            "mfe_avg": statistics.mean(mfe) if mfe else 0.0,
            "mae_avg": statistics.mean(mae) if mae else 0.0,
            "mfe1_rate": 100.0 * sum(1 for v in mfe if v >= 0.01) / len(mfe) if mfe else 0.0,
            "mfe2_rate": 100.0 * sum(1 for v in mfe if v >= 0.02) / len(mfe) if mfe else 0.0,
            "net_ci": ci,
            "pair_net": pair_net,
            "top_pair_share": top_positive / positive_total if positive_total > 0 else 1.0,
        }

    @staticmethod
    def _promote(s: dict[str, Any], min_n: int) -> bool:
        return bool(
            int(s.get("n", 0)) >= min_n
            and float(s.get("gross_exp", 0.0)) >= PROMOTE_GROSS
            and float(s.get("net_exp", 0.0)) >= PROMOTE_NET
            and float(s.get("positive_rate", 0.0)) >= 52.0
        )

    @staticmethod
    def _replication_pass(s: dict[str, Any]) -> bool:
        lo, _ = s.get("net_ci", (0.0, 0.0))
        return bool(
            int(s.get("n", 0)) >= MIN_REPLICATION
            and float(s.get("gross_exp", 0.0)) >= REPL_GROSS
            and float(s.get("net_exp", 0.0)) >= REPL_NET
            and float(s.get("positive_rate", 0.0)) >= 52.0
            and float(lo) > 0.0
        )

    @staticmethod
    def _quarter_stats(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        start = HOLDOUT_END
        out: list[dict[str, Any]] = []
        for q in range(4):
            a = start + pd.DateOffset(months=3 * q)
            b = start + pd.DateOffset(months=3 * (q + 1))
            part = [r for r in rows if a <= r["entry_time"] < b]
            out.append({
                "q": q + 1,
                "n": len(part),
                "net_exp": statistics.mean([float(r["net_r"]) for r in part]) if part else 0.0,
            })
        return out

    def run(self) -> dict[str, Any]:
        if self.already_done():
            return {"skipped": True, "version": AUDIT_VERSION}

        raw_by_pair: dict[str, pd.DataFrame] = {}
        coverage: list[dict[str, Any]] = []
        for symbol in SYMBOLS:
            raw, info = self._fetch_symbol(symbol)
            raw_by_pair[PAIR_NAMES[symbol]] = raw
            coverage.append(info)

        prepared: dict[tuple[str, str], tuple[pd.DataFrame, dict[str, tuple[pd.Series, str]]]] = {}
        event_names: list[str] | None = None
        for tf in TIMEFRAMES:
            for pair, raw in raw_by_pair.items():
                prepared[(pair, tf)] = self._prepare_events(raw, tf)
                if event_names is None:
                    event_names = list(prepared[(pair, tf)][1].keys())
        event_names = event_names or []

        rows_by_candidate: dict[str, list[dict[str, Any]]] = {}
        candidate_table: list[dict[str, Any]] = []
        validated: list[dict[str, Any]] = []
        seed = 88000

        for tf in TIMEFRAMES:
            for event_name in event_names:
                for horizon_h in HORIZONS_H:
                    cid = f"{tf}|{event_name}|{horizon_h}h"
                    rows = self._collect_candidate(prepared, tf, event_name, horizon_h)
                    rows_by_candidate[cid] = rows
                    dev_rows = [r for r in rows if r["phase"] == "DEV"]
                    val_rows = [r for r in rows if r["phase"] == "VAL"]
                    dev = self._stats(dev_rows, seed); seed += 1
                    val = self._stats(val_rows, seed); seed += 1
                    dev_pass = self._promote(dev, MIN_DEV)
                    val_pass = dev_pass and self._promote(val, MIN_VAL)
                    holdout: dict[str, Any] | None = None
                    holdout_pass = False
                    if val_pass:
                        hold_rows = [r for r in rows if r["phase"] == "HOLDOUT"]
                        holdout = self._stats(hold_rows, seed); seed += 1
                        holdout_pass = self._promote(holdout, MIN_HOLDOUT)
                    item = {
                        "id": cid,
                        "tf": tf,
                        "event": event_name,
                        "horizon_h": horizon_h,
                        "dev": dev,
                        "val": val,
                        "holdout": holdout,
                        "dev_pass": dev_pass,
                        "val_pass": val_pass,
                        "holdout_pass": holdout_pass,
                    }
                    candidate_table.append(item)
                    if holdout_pass:
                        validated.append(item)

        # External year is opened only for candidates that survived research-year HOLDOUT.
        replicated: list[dict[str, Any]] = []
        for item in validated:
            rows = rows_by_candidate[item["id"]]
            rep_rows = [r for r in rows if r["phase"] == "REPLICATION"]
            rep = self._stats(rep_rows, seed); seed += 1
            quarters = self._quarter_stats(rep_rows)
            quarter_positive = sum(1 for q in quarters if q["n"] > 0 and q["net_exp"] > 0)
            passed = self._replication_pass(rep) and quarter_positive >= 3
            item["replication"] = rep
            item["replication_quarters"] = quarters
            item["replication_positive_quarters"] = quarter_positive
            item["replication_pass"] = passed
            if passed:
                replicated.append(item)

        top_dev = sorted(
            [c for c in candidate_table if c["dev"]["n"] >= MIN_DEV],
            key=lambda c: (c["dev"]["net_exp"], c["dev"]["n"]),
            reverse=True,
        )[:8]
        top_movement = sorted(
            [c for c in candidate_table if c["dev"]["n"] >= MIN_DEV],
            key=lambda c: (c["dev"]["mfe_avg"] - c["dev"]["mae_avg"], c["dev"]["mfe_avg"]),
            reverse=True,
        )[:5]

        summary = {
            "version": AUDIT_VERSION,
            "coverage": coverage,
            "candidate_count": len(candidate_table),
            "event_count": len(event_names),
            "timeframes": list(TIMEFRAMES),
            "horizons_h": list(HORIZONS_H),
            "thresholds": {
                "promote_gross": PROMOTE_GROSS,
                "promote_net": PROMOTE_NET,
                "dev_n": MIN_DEV,
                "val_n": MIN_VAL,
                "holdout_n": MIN_HOLDOUT,
                "replication_n": MIN_REPLICATION,
            },
            "top_dev": top_dev,
            "top_movement": top_movement,
            "validated": validated,
            "replicated": replicated,
            "verdict": "EDGE_REPLICATED" if replicated else "NO_REPLICATED_EVENT_EDGE",
        }
        self._save_marker(summary)
        return summary

    @staticmethod
    def _fmt_candidate(c: dict[str, Any], phase: str) -> str:
        s = c.get(phase) or {}
        ci = s.get("net_ci", (0.0, 0.0))
        return (
            f"{c['tf']} · {c['event']} · {c['horizon_h']}h: "
            f"n={s.get('n',0)} · lordo {100*s.get('gross_exp',0):+.3f}% · "
            f"netto {100*s.get('net_exp',0):+.3f}% · hit+ {s.get('positive_rate',0):.1f}% · "
            f"MFE {100*s.get('mfe_avg',0):.2f}% · CI netto {100*ci[0]:+.3f}%→{100*ci[1]:+.3f}%"
        )

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        if s.get("skipped"):
            return ""
        lines = [
            "🔬 <b>EVENT EDGE DISCOVERY · 24 MESI</b>",
            "BTC/USDT · ETH/USDT · SOL/USDT · Binance Spot",
            "Obiettivo: trovare eventi con movimento direzionale abbastanza grande da sopravvivere ai costi.",
            "TF: 15m / 30m / 1h / 2h / 4h · orizzonti: 4h / 8h / 12h",
            f"Eventi predefiniti: <b>{s.get('event_count',0)}</b> · candidati pooled: <b>{s.get('candidate_count',0)}</b>",
            "Ricerca: 09/08/2024→09/08/2025 (DEV 6m · VAL 3m · HOLDOUT 3m)",
            "Replica esterna congelata: 09/08/2025→09/08/2026",
            "Gate ricerca: movimento lordo medio ≥0.50% · netto medio ≥0.10% · hit positivo ≥52%",
            "",
            "🏗 <b>Top DEV per drift netto</b>",
        ]
        for c in s.get("top_dev", [])[:8]:
            lines.append("• " + EventMoveDiscoveryBinance24mAudit._fmt_candidate(c, "dev"))
        if not s.get("top_dev"):
            lines.append("• Nessun candidato con campione DEV sufficiente.")

        lines += ["", "🌊 <b>Top DEV per asimmetria MFE−MAE</b>"]
        for c in s.get("top_movement", [])[:5]:
            d = c["dev"]
            lines.append(
                f"• {c['tf']} · {c['event']} · {c['horizon_h']}h: n={d['n']} · "
                f"MFE {100*d['mfe_avg']:.2f}% · MAE {100*d['mae_avg']:.2f}% · "
                f"P(MFE≥1%) {d['mfe1_rate']:.1f}% · P(MFE≥2%) {d['mfe2_rate']:.1f}%"
            )

        validated = s.get("validated", [])
        lines += ["", f"🧪 <b>Superano DEV + VAL + HOLDOUT:</b> {len(validated)}"]
        for c in validated[:6]:
            lines.append("• " + EventMoveDiscoveryBinance24mAudit._fmt_candidate(c, "holdout"))

        replicated = s.get("replicated", [])
        lines += ["", f"🔒 <b>Confermati anche nella replica 2025–2026:</b> {len(replicated)}"]
        for c in replicated[:6]:
            r = c.get("replication", {})
            lines.append(
                "• " + EventMoveDiscoveryBinance24mAudit._fmt_candidate(c, "replication")
                + f" · trimestri+ {c.get('replication_positive_quarters',0)}/4"
            )

        lines += ["", "📥 <b>Dati</b>"]
        for info in s.get("coverage", []):
            lines.append(
                f"• {info.get('pair')}: {info.get('bars',0)} barre · gap {info.get('missing_intervals',0)} · richieste {info.get('requests',0)}"
            )

        if replicated:
            lines += [
                "",
                "✅ <b>EVENTO CON EDGE REPLICATO</b>: almeno un evento supera ricerca, holdout e anno esterno.",
                "Il passo successivo è costruire una regola di entrata/uscita congelata attorno a quell’evento, senza usare la replica per ottimizzarla.",
            ]
        else:
            lines += [
                "",
                "❌ <b>NESSUN EVENTO DIREZIONALE REPLICATO</b> con i gate predefiniti.",
                "Il report MFE/MAE resta utile per capire se esistono movimenti ampi ma non persistenti, da studiare separatamente senza ritoccare questo test.",
            ]
        return "\n".join(lines)
