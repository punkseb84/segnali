"""36-month barrier-first path edge discovery on Binance Spot.

Parameter selection is isolated to DEV 2023-08-09 -> 2024-02-09.  For each of
18 predeclared events x 5 timeframes, only one of 24 fixed TP/SL/hold variants
may be selected.  The frozen variant must then survive VALIDATION, HOLDOUT and
two external years without any retuning.
"""
from __future__ import annotations

from typing import Any
import json
import math
import random
import statistics

import pandas as pd
import requests

from project.event_move_discovery_binance_24m_audit import (
    EventMoveDiscoveryBinance24mAudit,
    TIMEFRAMES,
    TF_MINUTES,
)
from project.cryptoresearch_v1_cand000006_binance_12m_v2_audit import ARCHIVE_BASE
from project.cryptoresearch_v1_cand000006_binance_12m_audit import SYMBOLS, PAIR_NAMES, INTERVAL

AUDIT_VERSION = "PATH_EDGE_DISCOVERY_BINANCE_36M_20260809_V2_DEV_ISOLATED"
FETCH_START = pd.Timestamp("2023-07-01T00:00:00Z")
DEV_START = pd.Timestamp("2023-08-09T00:00:00Z")
DEV_END = pd.Timestamp("2024-02-09T00:00:00Z")
VAL_END = pd.Timestamp("2024-05-09T00:00:00Z")
HOLDOUT_END = pd.Timestamp("2024-08-09T00:00:00Z")
EXTERNAL_A_END = pd.Timestamp("2025-08-09T00:00:00Z")
EXTERNAL_B_END = pd.Timestamp("2026-08-09T00:00:00Z")

BARRIERS: tuple[tuple[str, float, float], ...] = (
    ("TP075_SL050", 0.0075, 0.0050),
    ("TP100_SL050", 0.0100, 0.0050),
    ("TP100_SL075", 0.0100, 0.0075),
    ("TP150_SL075", 0.0150, 0.0075),
    ("TP150_SL100", 0.0150, 0.0100),
    ("TP200_SL100", 0.0200, 0.0100),
    ("TP200_SL150", 0.0200, 0.0150),
    ("TP250_SL150", 0.0250, 0.0150),
)
MAX_HOLDS_H = (4, 8, 12)
MIN_DEV = 30
MIN_VAL = 15
MIN_HOLDOUT = 15
MIN_EXTERNAL = 25
DEV_MIN_PF = 1.15
DEV_MIN_EXP = 0.0010
FINAL_MIN_PF = 1.10
FINAL_MIN_EXP = 0.0005


class PathEdgeDiscoveryBinance36mAudit(EventMoveDiscoveryBinance24mAudit):
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
        for period in pd.period_range("2023-07", "2026-07", freq="M"):
            ym = period.strftime("%Y-%m")
            urls.append(f"{ARCHIVE_BASE}/monthly/klines/{symbol}/{INTERVAL}/{symbol}-{INTERVAL}-{ym}.zip")
        for day in pd.date_range("2026-08-01", "2026-08-08", freq="D", tz="UTC"):
            ymd = day.strftime("%Y-%m-%d")
            urls.append(f"{ARCHIVE_BASE}/daily/klines/{symbol}/{INTERVAL}/{symbol}-{INTERVAL}-{ymd}.zip")
        return urls

    def _fetch_symbol(self, symbol: str) -> tuple[pd.DataFrame, dict[str, Any]]:
        frames: list[pd.DataFrame] = []
        session = requests.Session()
        session.headers.update({"User-Agent": "path-edge-discovery/2.0"})
        urls = self._archive_urls(symbol)
        for url in urls:
            response = session.get(url, timeout=30)
            if response.status_code != 200:
                raise RuntimeError(
                    f"BINANCE_ARCHIVE_HTTP symbol={symbol} status={response.status_code} file={url.rsplit('/',1)[-1]}"
                )
            frames.append(self._read_zip(response.content))
        frame = pd.concat(frames, ignore_index=True)
        frame = frame[(frame["timestamp"] >= FETCH_START) & (frame["timestamp"] < EXTERNAL_B_END)]
        frame = frame.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
        if len(frame) < 105000:
            raise RuntimeError(f"BINANCE_36M_HISTORY_TOO_SHORT symbol={symbol} bars={len(frame)}")
        diffs = frame["timestamp"].diff().dropna().dt.total_seconds().div(900.0)
        missing = int(sum(max(0, int(round(v)) - 1) for v in diffs if v > 1.0))
        return frame, {
            "symbol": symbol, "pair": PAIR_NAMES[symbol], "bars": len(frame),
            "from": str(frame.iloc[0]["timestamp"]), "to": str(frame.iloc[-1]["timestamp"]),
            "requests": len(urls), "missing_intervals": missing,
            "transport": "BINANCE_VISION_ARCHIVES",
        }

    @staticmethod
    def _phase(ts: pd.Timestamp) -> str | None:
        if ts < DEV_START or ts >= EXTERNAL_B_END:
            return None
        if ts < DEV_END:
            return "DEV"
        if ts < VAL_END:
            return "VAL"
        if ts < HOLDOUT_END:
            return "HOLDOUT"
        if ts < EXTERNAL_A_END:
            return "EXTERNAL_A"
        return "EXTERNAL_B"

    @staticmethod
    def _resample36(raw: pd.DataFrame, tf: str) -> pd.DataFrame:
        if tf == "15m":
            return raw[["timestamp", "open", "high", "low", "close", "volume"]].copy().sort_values(
                "timestamp"
            ).drop_duplicates("timestamp").reset_index(drop=True)
        x = raw.set_index("timestamp").resample(
            TIMEFRAMES[tf], origin="epoch", label="left", closed="left"
        ).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().reset_index()
        return x[(x["timestamp"] + pd.Timedelta(minutes=TF_MINUTES[tf])) <= EXTERNAL_B_END].reset_index(drop=True)

    def _prepare36(self, raw: pd.DataFrame, tf: str) -> tuple[pd.DataFrame, dict[str, tuple[pd.Series, str]]]:
        x = self._resample36(raw, tf)
        op, hi, lo = x["open"].astype(float), x["high"].astype(float), x["low"].astype(float)
        cl, vol = x["close"].astype(float), x["volume"].astype(float)
        prev_close = cl.shift(1)
        tr = pd.concat([hi - lo, (hi - prev_close).abs(), (lo - prev_close).abs()], axis=1).max(axis=1)
        atr20 = tr.rolling(20).mean()
        rvol20 = vol / vol.rolling(20).mean().replace(0.0, math.nan)
        rng = (hi - lo).replace(0.0, math.nan)
        close_loc = (cl - lo) / rng
        prev_high20, prev_low20 = hi.rolling(20).max().shift(1), lo.rolling(20).min().shift(1)
        prev_high50, prev_low50 = hi.rolling(50).max().shift(1), lo.rolling(50).min().shift(1)
        mom4_bars = max(1, int(round(240 / TF_MINUTES[tf])))
        mom4h = cl / cl.shift(mom4_bars) - 1.0
        ret1 = cl / prev_close - 1.0
        channel_width = (prev_high20 - prev_low20) / prev_close.replace(0.0, math.nan)
        compressed = channel_width <= channel_width.rolling(100).quantile(0.25).shift(1)
        range_exp = tr / atr20.replace(0.0, math.nan) >= 1.5
        vol_burst = rvol20 >= 2.5
        range_vol = range_exp & (rvol20 >= 2.0)
        return x, {
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

    def _simulate_pair_candidate(
        self, x: pd.DataFrame, mask: pd.Series, direction: str, pair: str, tf: str,
        event_name: str, tp_pct: float, sl_pct: float, max_hold_h: int,
        phase_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        trades: list[dict[str, Any]] = []
        max_bars = max(1, int(math.ceil(max_hold_h * 60 / TF_MINUTES[tf])))
        blocked_until = -1
        for signal_i in [int(i) for i in x.index[mask.fillna(False)]]:
            entry_i = signal_i + 1
            if entry_i <= blocked_until or entry_i >= len(x):
                continue
            entry_time = pd.Timestamp(x.iloc[entry_i]["timestamp"])
            phase = self._phase(entry_time)
            if phase is None or (phase_filter is not None and phase != phase_filter):
                continue
            entry = float(x.iloc[entry_i]["open"])
            if not math.isfinite(entry) or entry <= 0:
                continue
            target = entry * (1.0 + tp_pct) if direction == "LONG" else entry * (1.0 - tp_pct)
            stop = entry * (1.0 - sl_pct) if direction == "LONG" else entry * (1.0 + sl_pct)
            last_i = min(len(x) - 1, entry_i + max_bars - 1)
            if pd.Timestamp(x.iloc[last_i]["timestamp"]) >= EXTERNAL_B_END:
                break
            exit_px: float | None = None
            reason, exit_i = "TIMEOUT", last_i
            for k in range(entry_i, last_i + 1):
                row = x.iloc[k]
                op, hi, lo = float(row["open"]), float(row["high"]), float(row["low"])
                if direction == "LONG":
                    if op <= stop: exit_px, reason, exit_i = op, "STOP_GAP", k; break
                    if op >= target: exit_px, reason, exit_i = op, "TARGET_GAP", k; break
                    if lo <= stop: exit_px, reason, exit_i = stop, "STOP", k; break
                    if hi >= target: exit_px, reason, exit_i = target, "TARGET", k; break
                else:
                    if op >= stop: exit_px, reason, exit_i = op, "STOP_GAP", k; break
                    if op <= target: exit_px, reason, exit_i = op, "TARGET_GAP", k; break
                    if hi >= stop: exit_px, reason, exit_i = stop, "STOP", k; break
                    if lo <= target: exit_px, reason, exit_i = target, "TARGET", k; break
            if exit_px is None:
                exit_px = float(x.iloc[last_i]["close"])
            gross, fee_only, net = self._net_return(entry, float(exit_px), direction)
            trades.append({
                "pair": pair, "tf": tf, "event": event_name, "direction": direction,
                "tp_pct": tp_pct, "sl_pct": sl_pct, "max_hold_h": max_hold_h,
                "entry_time": entry_time, "exit_time": pd.Timestamp(x.iloc[exit_i]["timestamp"]),
                "phase": phase, "gross_r": gross, "fee_r": fee_only, "net_r": net,
                "net_eur": self.notional * net, "reason": reason, "bars": exit_i - entry_i + 1,
            })
            blocked_until = exit_i
        return trades

    @staticmethod
    def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
        net, gross = [float(r["net_r"]) for r in rows], [float(r["gross_r"]) for r in rows]
        wins, losses = [v for v in net if v > 0], [v for v in net if v < 0]
        gp, gl = sum(wins), abs(sum(losses))
        pair_pnl: dict[str, float] = {}
        reasons: dict[str, int] = {}
        for r in rows:
            pair_pnl[r["pair"]] = pair_pnl.get(r["pair"], 0.0) + float(r["net_eur"])
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        positive = sum(v for v in pair_pnl.values() if v > 0)
        top = max([v for v in pair_pnl.values() if v > 0], default=0.0)
        return {
            "n": len(rows), "wr": 100.0 * len(wins) / len(rows) if rows else 0.0,
            "pf": gp / gl if gl else (999.0 if gp else 0.0),
            "gross_exp": statistics.mean(gross) if gross else 0.0,
            "exp": statistics.mean(net) if net else 0.0,
            "net_eur": sum(float(r["net_eur"]) for r in rows),
            "avg_bars": statistics.mean([int(r["bars"]) for r in rows]) if rows else 0.0,
            "pair_pnl": pair_pnl, "top_pair_share": top / positive if positive > 0 else 1.0,
            "reasons": reasons,
        }

    @staticmethod
    def _bootstrap(rows: list[dict[str, Any]], seed: int, nboot: int = 3000) -> tuple[float, float]:
        vals = [float(r["net_r"]) for r in rows]
        if len(vals) < 10:
            return (0.0, 0.0)
        rng, n = random.Random(seed), len(vals)
        means = [sum(vals[rng.randrange(n)] for _ in range(n)) / n for _ in range(nboot)]
        means.sort()
        return means[int(0.025 * nboot)], means[min(nboot - 1, int(0.975 * nboot))]

    @staticmethod
    def _phase_rows(rows: list[dict[str, Any]], phase: str) -> list[dict[str, Any]]:
        return [r for r in rows if r["phase"] == phase]

    def _run_frozen_candidate(
        self, prepared: dict[tuple[str, str], tuple[pd.DataFrame, dict[str, tuple[pd.Series, str]]]],
        tf: str, event_name: str, tp_pct: float, sl_pct: float, hold_h: int,
    ) -> tuple[list[dict[str, Any]], str]:
        rows: list[dict[str, Any]] = []
        direction = "LONG"
        for pair in PAIR_NAMES.values():
            x, events = prepared[(pair, tf)]
            mask, direction = events[event_name]
            rows.extend(self._simulate_pair_candidate(
                x, mask, direction, pair, tf, event_name, tp_pct, sl_pct, hold_h
            ))
        return rows, direction

    def run(self) -> dict[str, Any]:
        if self.already_done():
            return {"skipped": True, "version": AUDIT_VERSION}

        coverage: list[dict[str, Any]] = []
        raw_by_pair: dict[str, pd.DataFrame] = {}
        for symbol in SYMBOLS:
            raw, info = self._fetch_symbol(symbol)
            raw_by_pair[PAIR_NAMES[symbol]] = raw
            coverage.append(info)

        prepared: dict[tuple[str, str], tuple[pd.DataFrame, dict[str, tuple[pd.Series, str]]]] = {}
        for pair, raw in raw_by_pair.items():
            for tf in TIMEFRAMES:
                prepared[(pair, tf)] = self._prepare36(raw, tf)
        event_names = list(next(iter(prepared.values()))[1].keys())

        family_rows: list[dict[str, Any]] = []
        dev_qualified = passed_val = passed_holdout = passed_external_a = 0
        confirmed: list[dict[str, Any]] = []

        for tf in TIMEFRAMES:
            for event_name in event_names:
                best: dict[str, Any] | None = None
                # Expensive grid is DEV-only. No later phase is evaluated here.
                for barrier_name, tp_pct, sl_pct in BARRIERS:
                    for hold_h in MAX_HOLDS_H:
                        dev_rows: list[dict[str, Any]] = []
                        direction = "LONG"
                        for pair in PAIR_NAMES.values():
                            x, events = prepared[(pair, tf)]
                            mask, direction = events[event_name]
                            dev_rows.extend(self._simulate_pair_candidate(
                                x, mask, direction, pair, tf, event_name,
                                tp_pct, sl_pct, hold_h, phase_filter="DEV"
                            ))
                        dev = self._stats(dev_rows)
                        if dev["n"] < MIN_DEV:
                            continue
                        candidate = {
                            "id": f"{tf}_{event_name}_{barrier_name}_{hold_h}h",
                            "tf": tf, "event": event_name, "direction": direction,
                            "barrier": barrier_name, "tp_pct": tp_pct, "sl_pct": sl_pct,
                            "hold_h": hold_h, "dev": dev, "dev_rows": dev_rows,
                        }
                        if best is None or (dev["exp"], dev["pf"], dev["n"]) > (
                            best["dev"]["exp"], best["dev"]["pf"], best["dev"]["n"]
                        ):
                            best = candidate
                if best is None:
                    continue

                dev_ci = self._bootstrap(best["dev_rows"], seed=310000 + len(family_rows))
                best["dev_ci"] = dev_ci
                best["dev_pass"] = bool(
                    best["dev"]["pf"] >= DEV_MIN_PF and best["dev"]["exp"] >= DEV_MIN_EXP and dev_ci[0] > 0.0
                )
                if best["dev_pass"]: dev_qualified += 1

                # Only the one DEV-selected variant is now exposed to later periods.
                all_rows, direction = self._run_frozen_candidate(
                    prepared, tf, event_name, best["tp_pct"], best["sl_pct"], best["hold_h"]
                )
                best["direction"] = direction
                val = self._stats(self._phase_rows(all_rows, "VAL"))
                holdout = self._stats(self._phase_rows(all_rows, "HOLDOUT"))
                ext_a = self._stats(self._phase_rows(all_rows, "EXTERNAL_A"))
                ext_b_rows = self._phase_rows(all_rows, "EXTERNAL_B")
                ext_b = self._stats(ext_b_rows)
                best.update({"val": val, "holdout": holdout, "external_a": ext_a, "external_b": ext_b})

                best["val_pass"] = bool(best["dev_pass"] and val["n"] >= MIN_VAL and val["pf"] > 1.0 and val["exp"] > 0.0)
                if best["val_pass"]: passed_val += 1
                best["holdout_pass"] = bool(
                    best["val_pass"] and holdout["n"] >= MIN_HOLDOUT and holdout["pf"] > 1.0 and holdout["exp"] > 0.0
                )
                if best["holdout_pass"]: passed_holdout += 1
                best["external_a_pass"] = bool(
                    best["holdout_pass"] and ext_a["n"] >= MIN_EXTERNAL and ext_a["pf"] > 1.0 and ext_a["exp"] > 0.0
                )
                if best["external_a_pass"]: passed_external_a += 1

                ext_b_ci = self._bootstrap(ext_b_rows, seed=410000 + len(family_rows)) if best["external_a_pass"] else (0.0, 0.0)
                best["external_b_ci"] = ext_b_ci
                best["confirmed"] = bool(
                    best["external_a_pass"] and ext_b["n"] >= MIN_EXTERNAL
                    and ext_b["pf"] >= FINAL_MIN_PF and ext_b["exp"] >= FINAL_MIN_EXP
                    and ext_b_ci[0] > 0.0 and ext_b["top_pair_share"] <= 0.70
                )
                if best["confirmed"]:
                    confirmed.append({k: v for k, v in best.items() if k != "dev_rows"})
                family_rows.append({k: v for k, v in best.items() if k != "dev_rows"})

        top_dev = sorted(family_rows, key=lambda r: (float(r["dev"]["exp"]), float(r["dev"]["pf"])), reverse=True)[:8]
        finalists = sorted(
            [r for r in family_rows if r.get("external_a_pass")],
            key=lambda r: (float(r["external_b"]["exp"]), float(r["external_b"]["pf"])), reverse=True,
        )[:8]
        summary = {
            "version": AUDIT_VERSION, "events": len(event_names), "timeframes": list(TIMEFRAMES.keys()),
            "barriers": [{"name": n, "tp": tp, "sl": sl} for n, tp, sl in BARRIERS],
            "holds_h": list(MAX_HOLDS_H),
            "raw_grid": len(event_names) * len(TIMEFRAMES) * len(BARRIERS) * len(MAX_HOLDS_H),
            "families": len(event_names) * len(TIMEFRAMES),
            "dev_qualified": dev_qualified, "passed_val": passed_val,
            "passed_holdout": passed_holdout, "passed_external_a": passed_external_a,
            "confirmed": confirmed, "top_dev": top_dev, "finalists": finalists,
            "coverage": coverage,
            "costs": {"buy": self.buy_fee, "sell": self.sell_fee, "spread": self.spread, "slippage": self.slippage},
        }
        self._save_marker(summary)
        return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        if s.get("skipped"):
            return ""
        lines = [
            "🧭 <b>PATH EDGE DISCOVERY · 36 MESI</b>",
            "BTC/USDT · ETH/USDT · SOL/USDT · Binance Spot candles",
            "Dopo un evento: target, stop o timeout — quale arriva prima con expectancy replicabile?",
            f"Eventi: <b>{s.get('events',0)}</b> · TF: 15m/30m/1h/2h/4h · griglia DEV: <b>{s.get('raw_grid',0)}</b>",
            "Una sola configurazione per evento×TF viene scelta sul DEV; poi è congelata.",
            "DEV 08/2023→02/2024 · VAL 02→05/2024 · HOLDOUT 05→08/2024",
            "EXTERNAL A 08/2024→08/2025 · EXTERNAL B 08/2025→08/2026",
            f"Costi: fee {100*s['costs']['buy']:.3f}% + {100*s['costs']['sell']:.3f}% · spread {100*s['costs']['spread']:.3f}%",
            "", "🏗 <b>Top configurazioni scelte SOLO sul DEV</b>",
        ]
        for r in s.get("top_dev", [])[:8]:
            d, ci = r["dev"], r.get("dev_ci", (0.0, 0.0))
            lines.append(
                f"• {r['tf']} · {r['event']} · TP {100*r['tp_pct']:.2f}% / SL {100*r['sl_pct']:.2f}% / {r['hold_h']}h: "
                f"n={d['n']} PF {d['pf']:.2f} Exp {100*d['exp']:+.3f}% · CI {100*ci[0]:+.3f}%→{100*ci[1]:+.3f}%"
            )
        lines += [
            "", "🧪 <b>Funnel di replicazione</b>",
            f"Famiglie evento×TF: <b>{s.get('families',0)}</b>",
            f"Passano DEV forte: <b>{s.get('dev_qualified',0)}</b>",
            f"Passano anche VALIDATION: <b>{s.get('passed_val',0)}</b>",
            f"Passano anche HOLDOUT: <b>{s.get('passed_holdout',0)}</b>",
            f"Passano anche EXTERNAL A: <b>{s.get('passed_external_a',0)}</b>",
            f"Confermati su EXTERNAL B: <b>{len(s.get('confirmed',[]))}</b>",
        ]
        if s.get("finalists"):
            lines += ["", "🔒 <b>Candidati arrivati alla replica finale</b>"]
            for r in s["finalists"][:6]:
                a, b, ci = r["external_a"], r["external_b"], r.get("external_b_ci", (0.0, 0.0))
                lines.append(
                    f"• {r['tf']} · {r['event']} · TP {100*r['tp_pct']:.2f}% / SL {100*r['sl_pct']:.2f}% / {r['hold_h']}h · "
                    f"A n={a['n']} PF {a['pf']:.2f} Exp {100*a['exp']:+.3f}% · "
                    f"B n={b['n']} PF {b['pf']:.2f} Exp {100*b['exp']:+.3f}% CI {100*ci[0]:+.3f}%→{100*ci[1]:+.3f}%"
                )
        lines += ["", "📥 <b>Dati Binance</b>"]
        for c in s.get("coverage", []):
            lines.append(f"• {c['pair']}: {c['bars']} barre · gap {c['missing_intervals']} · richieste {c['requests']}")
        if s.get("confirmed"):
            lines += ["", "✅ <b>PATH EDGE REPLICATO</b>: almeno una configurazione sopravvive ai due anni esterni.",
                      "Prima del live serve un audit portfolio/esecuzione separato."]
        else:
            lines += ["", "❌ <b>NESSUN PATH EDGE REPLICATO</b> con eventi, barriere e costi predefiniti.",
                      "Nessun parametro viene ritoccato usando VAL, HOLDOUT o gli anni esterni."]
        lines.append("⚠️ Gli eventi SHORT sono teorici su Spot; short reale richiede margin/futures.")
        return "\n".join(lines)
