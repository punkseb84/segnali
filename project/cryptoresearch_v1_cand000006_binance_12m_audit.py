"""Frozen 12-month Binance Spot audit for CryptoResearchV1 / CAND000006.

The strategy parameters are NOT optimized here.  The study window is fixed in
advance: 2025-08-09 00:00 UTC -> 2026-08-09 00:00 UTC, with extra warm-up data
from 2025-07-01.  Only BTC/USDT, ETH/USDT and SOL/USDT are tested, matching the
original Freqtrade universe.  DEV / VALIDATION / TEST are fixed chronologically
at 6 / 3 / 3 months.  TEST is not used to select or modify parameters.

Public Binance Spot klines are fetched from the official market-data REST API.
Freqtrade-like assumptions used: signal on a closed 15m candle, entry at the
next candle open, stoploss before ROI if both occur in one candle, ROI evaluated
against candle high/open.  ROI decisions include trading fees; the reported
realistic economic return additionally includes the configured spread.
"""
from __future__ import annotations

from typing import Any
import json
import math
import random
import statistics
import time

import pandas as pd
import requests

from project.cryptoresearch_v1_cand000006_audit import CryptoResearchV1Cand000006Audit

AUDIT_VERSION = "CRYPTORESEARCH_V1_CAND000006_BINANCE_12M_20260809_V1"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
PAIR_NAMES = {"BTCUSDT": "BTC/USDT", "ETHUSDT": "ETH/USDT", "SOLUSDT": "SOL/USDT"}
BASE_URLS = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
)
INTERVAL = "15m"
INTERVAL_MS = 15 * 60 * 1000
FETCH_START = pd.Timestamp("2025-07-01T00:00:00Z")
STUDY_START = pd.Timestamp("2025-08-09T00:00:00Z")
DEV_END = pd.Timestamp("2026-02-09T00:00:00Z")
VAL_END = pd.Timestamp("2026-05-09T00:00:00Z")
STUDY_END = pd.Timestamp("2026-08-09T00:00:00Z")
MIN_FETCH_BARS = 36000
STOPLOSS = -0.03
ROI_SCHEDULE = ((720, 0.0), (480, 0.005), (240, 0.015), (0, 0.025))


class CryptoResearchV1Cand000006Binance12mAudit(CryptoResearchV1Cand000006Audit):
    """One-shot, frozen, long-history robustness audit."""

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
    def _ms(ts: pd.Timestamp) -> int:
        return int(ts.timestamp() * 1000)

    def _request_klines(self, session: requests.Session, symbol: str, start_ms: int, end_ms: int) -> list[list[Any]]:
        params = {
            "symbol": symbol,
            "interval": INTERVAL,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000,
        }
        last_error = "unknown"
        for base in BASE_URLS:
            for attempt in range(4):
                try:
                    response = session.get(f"{base}/api/v3/klines", params=params, timeout=20)
                    if response.status_code == 200:
                        payload = response.json()
                        if isinstance(payload, list):
                            return payload
                        last_error = f"non-list response from {base}"
                        break
                    last_error = f"HTTP {response.status_code} {response.text[:160]}"
                    if response.status_code in (418, 429):
                        wait = response.headers.get("Retry-After")
                        delay = min(30.0, float(wait)) if wait else min(8.0, 1.5 * (attempt + 1))
                        time.sleep(delay)
                    elif response.status_code >= 500:
                        time.sleep(1.0 + attempt)
                    else:
                        break
                except requests.RequestException as exc:
                    last_error = repr(exc)
                    time.sleep(1.0 + attempt)
        raise RuntimeError(f"BINANCE_KLINES_FAILED symbol={symbol} start={start_ms} error={last_error}")

    def _fetch_symbol(self, symbol: str) -> tuple[pd.DataFrame, dict[str, Any]]:
        start_ms = self._ms(FETCH_START)
        end_ms = self._ms(STUDY_END) - 1
        cursor = start_ms
        rows: list[list[Any]] = []
        requests_count = 0
        session = requests.Session()
        session.headers.update({"User-Agent": "crypto-research-lab/1.0"})
        while cursor <= end_ms:
            batch = self._request_klines(session, symbol, cursor, end_ms)
            requests_count += 1
            if not batch:
                break
            rows.extend(batch)
            last_open = int(batch[-1][0])
            nxt = last_open + INTERVAL_MS
            if nxt <= cursor:
                raise RuntimeError(f"BINANCE_PAGINATION_STALLED symbol={symbol} cursor={cursor}")
            cursor = nxt
            if len(batch) < 1000:
                break
            time.sleep(0.04)

        if not rows:
            raise RuntimeError(f"BINANCE_NO_DATA symbol={symbol}")
        frame = pd.DataFrame(rows, columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "trade_count", "taker_base", "taker_quote", "ignore",
        ])
        frame["timestamp"] = pd.to_datetime(frame["open_time"].astype("int64"), unit="ms", utc=True)
        for col in ("open", "high", "low", "close", "volume"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame = frame[["timestamp", "open", "high", "low", "close", "volume"]]
        frame = frame[(frame["timestamp"] >= FETCH_START) & (frame["timestamp"] < STUDY_END)]
        frame = frame.dropna().drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)

        if len(frame) < MIN_FETCH_BARS:
            raise RuntimeError(f"BINANCE_HISTORY_TOO_SHORT symbol={symbol} bars={len(frame)}")
        diffs = frame["timestamp"].diff().dropna().dt.total_seconds().div(900.0)
        missing = int(sum(max(0, int(round(v)) - 1) for v in diffs if v > 1.0))
        info = {
            "symbol": symbol,
            "pair": PAIR_NAMES[symbol],
            "bars": len(frame),
            "from": str(frame.iloc[0]["timestamp"]),
            "to": str(frame.iloc[-1]["timestamp"]),
            "requests": requests_count,
            "missing_intervals": missing,
        }
        return frame, info

    def _fee_return(self, entry: float, exit_px: float) -> float:
        q = exit_px / entry
        return q - 1.0 - self.buy_fee - q * self.sell_fee

    def _economic_return(self, entry: float, exit_px: float) -> float:
        q = exit_px / entry
        friction = (1.0 + q) * (self.spread / 2.0 + self.slippage)
        return self._fee_return(entry, exit_px) - friction

    def _roi_price_fee_only(self, entry: float, roi: float) -> float:
        # q - 1 - buy_fee - q*sell_fee = roi
        return entry * (1.0 + self.buy_fee + roi) / (1.0 - self.sell_fee)

    @staticmethod
    def _roi_for_minutes(minutes: int) -> float:
        for threshold, roi in ROI_SCHEDULE:
            if minutes >= threshold:
                return roi
        return 0.025

    @staticmethod
    def _phase(ts: pd.Timestamp) -> str | None:
        if ts < STUDY_START or ts >= STUDY_END:
            return None
        if ts < DEV_END:
            return "DEV"
        if ts < VAL_END:
            return "VAL"
        return "TEST"

    def _simulate_pair(self, raw: pd.DataFrame, pair: str) -> tuple[list[dict[str, Any]], int, int]:
        x = self._attach_safe_1h(self._indicators_15m(raw))
        trades: list[dict[str, Any]] = []
        signals = 0
        unresolved = 0
        i = 1
        while i < len(x) - 1:
            signal_time = x.iloc[i]["timestamp"]
            if signal_time < STUDY_START:
                i += 1
                continue
            if signal_time >= STUDY_END:
                break
            if not self._is_entry(x, i):
                i += 1
                continue
            signals += 1
            entry_i = i + 1
            entry_time = x.iloc[entry_i]["timestamp"]
            if entry_time >= STUDY_END:
                break
            entry = float(x.iloc[entry_i]["open"])
            stop_px = entry * (1.0 + STOPLOSS)
            closed: dict[str, Any] | None = None
            for k in range(entry_i, len(x)):
                row = x.iloc[k]
                ts = row["timestamp"]
                if ts >= STUDY_END:
                    break
                low = float(row["low"])
                high = float(row["high"])
                open_px = float(row["open"])
                minutes = int((ts - entry_time).total_seconds() // 60)
                roi = self._roi_for_minutes(minutes)
                roi_px = self._roi_price_fee_only(entry, roi)
                if low <= stop_px:
                    exit_px = stop_px
                    reason = "STOPLOSS"
                elif open_px >= roi_px:
                    exit_px = open_px
                    reason = "ROI_OPEN"
                elif high >= roi_px:
                    exit_px = roi_px
                    reason = "ROI"
                else:
                    continue
                gross_r = exit_px / entry - 1.0
                fee_r = self._fee_return(entry, exit_px)
                net_r = self._economic_return(entry, exit_px)
                closed = {
                    "pair": pair,
                    "entry_time": entry_time,
                    "exit_time": ts,
                    "phase": self._phase(entry_time),
                    "entry": entry,
                    "exit": exit_px,
                    "gross_r": gross_r,
                    "fee_r": fee_r,
                    "net_r": net_r,
                    "net_eur": self.notional * net_r,
                    "reason": reason,
                    "bars": k - entry_i + 1,
                }
                break
            if closed is None:
                unresolved += 1
                break
            trades.append(closed)
            exit_ts = closed["exit_time"]
            while i < len(x) - 1 and x.iloc[i]["timestamp"] <= exit_ts:
                i += 1
        return trades, signals, unresolved

    @staticmethod
    def _stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
        net = [float(t["net_r"]) for t in trades]
        fee = [float(t["fee_r"]) for t in trades]
        gross = [float(t["gross_r"]) for t in trades]
        wins = [v for v in net if v > 0]
        losses = [v for v in net if v < 0]
        gp = sum(wins)
        gl = abs(sum(losses))
        eq = peak = dd = 0.0
        reasons: dict[str, int] = {}
        pair_pnl: dict[str, float] = {}
        for t in sorted(trades, key=lambda z: z["exit_time"]):
            eq += float(t["net_r"])
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
            reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
            pair_pnl[t["pair"]] = pair_pnl.get(t["pair"], 0.0) + float(t["net_eur"])
        positive = sum(v for v in pair_pnl.values() if v > 0)
        top = max([v for v in pair_pnl.values() if v > 0], default=0.0)
        return {
            "n": len(trades),
            "wr": 100.0 * len(wins) / len(trades) if trades else 0.0,
            "pf": gp / gl if gl else (999.0 if gp else 0.0),
            "gross_exp": statistics.mean(gross) if gross else 0.0,
            "fee_exp": statistics.mean(fee) if fee else 0.0,
            "exp": statistics.mean(net) if net else 0.0,
            "net": sum(net),
            "net_eur": sum(float(t["net_eur"]) for t in trades),
            "max_dd": dd,
            "avg_bars": statistics.mean([int(t["bars"]) for t in trades]) if trades else 0.0,
            "reasons": reasons,
            "pair_pnl": pair_pnl,
            "top_pair_share": top / positive if positive > 0 else 1.0,
        }

    @staticmethod
    def _bootstrap(trades: list[dict[str, Any]], seed: int = 600006, nboot: int = 5000) -> tuple[float, float]:
        vals = [float(t["net_r"]) for t in trades]
        if len(vals) < 10:
            return (0.0, 0.0)
        rng = random.Random(seed)
        n = len(vals)
        means = []
        for _ in range(nboot):
            means.append(sum(vals[rng.randrange(n)] for _ in range(n)) / n)
        means.sort()
        return means[int(0.025 * nboot)], means[min(nboot - 1, int(0.975 * nboot))]

    @classmethod
    def _monthly(cls, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for t in trades:
            key = pd.Timestamp(t["entry_time"]).strftime("%Y-%m")
            groups.setdefault(key, []).append(t)
        return [{"month": key, **cls._stats(groups[key])} for key in sorted(groups)]

    def run(self) -> dict[str, Any]:
        if self.already_done():
            return {"skipped": True, "version": AUDIT_VERSION}

        all_trades: list[dict[str, Any]] = []
        coverage: list[dict[str, Any]] = []
        signal_counts: dict[str, int] = {}
        unresolved_counts: dict[str, int] = {}
        for symbol in SYMBOLS:
            raw, info = self._fetch_symbol(symbol)
            pair = PAIR_NAMES[symbol]
            trades, signals, unresolved = self._simulate_pair(raw, pair)
            all_trades.extend(trades)
            coverage.append(info)
            signal_counts[pair] = signals
            unresolved_counts[pair] = unresolved

        by_phase: dict[str, list[dict[str, Any]]] = {
            phase: [t for t in all_trades if t["phase"] == phase]
            for phase in ("DEV", "VAL", "TEST")
        }
        phases = {phase: self._stats(ts) for phase, ts in by_phase.items()}
        test_ci = self._bootstrap(by_phase["TEST"], seed=600009)
        full = self._stats(all_trades)
        full_ci = self._bootstrap(all_trades, seed=600010)
        monthly = self._monthly(all_trades)
        positive_months = sum(1 for m in monthly if float(m["net"]) > 0)

        per_pair = {}
        for pair in PAIR_NAMES.values():
            pts = [t for t in all_trades if t["pair"] == pair]
            per_pair[pair] = {
                "ALL": self._stats(pts),
                "DEV": self._stats([t for t in pts if t["phase"] == "DEV"]),
                "VAL": self._stats([t for t in pts if t["phase"] == "VAL"]),
                "TEST": self._stats([t for t in pts if t["phase"] == "TEST"]),
            }

        dev = phases["DEV"]
        val = phases["VAL"]
        test = phases["TEST"]
        enough = test["n"] >= 30 and full["n"] >= 100
        gate = (
            enough
            and dev["pf"] > 1.0 and dev["exp"] > 0
            and val["pf"] > 1.0 and val["exp"] > 0
            and test["pf"] > 1.10 and test["exp"] > 0
            and test_ci[0] > 0
            and positive_months >= 8
            and full["top_pair_share"] < 0.60
        )
        verdict = "EDGE_CONFIRMED" if gate else ("SAMPLE_INSUFFICIENT" if not enough else "EDGE_NOT_CONFIRMED")

        summary = {
            "version": AUDIT_VERSION,
            "strategy": "CryptoResearchV1 / CAND000006",
            "source": "Binance Spot public klines",
            "symbols": list(SYMBOLS),
            "study": {
                "fetch_start": str(FETCH_START),
                "study_start": str(STUDY_START),
                "dev_end": str(DEV_END),
                "val_end": str(VAL_END),
                "study_end": str(STUDY_END),
            },
            "notional": self.notional,
            "costs": {"buy": self.buy_fee, "sell": self.sell_fee, "spread": self.spread, "slippage": self.slippage},
            "coverage": coverage,
            "signals": signal_counts,
            "unresolved": unresolved_counts,
            "phases": phases,
            "full": full,
            "test_ci": test_ci,
            "full_ci": full_ci,
            "monthly": monthly,
            "positive_months": positive_months,
            "per_pair": per_pair,
            "verdict": verdict,
            "gates": {
                "test_n_min": 30,
                "full_n_min": 100,
                "dev_pf_gt": 1.0,
                "val_pf_gt": 1.0,
                "test_pf_gt": 1.10,
                "test_bootstrap_lower_gt": 0.0,
                "positive_months_min": 8,
                "top_pair_share_lt": 0.60,
            },
            "benchmark_old_oos": {"n": 41, "wr": 68.29, "pf": 1.041, "exp": 0.00028},
        }
        self._save_marker(summary)
        return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        if s.get("skipped"):
            return ""
        phases = s["phases"]
        full = s["full"]
        tci = s["test_ci"]
        fci = s["full_ci"]
        lines = [
            "🧬 <b>CAND000006 · BINANCE 12 MESI CONGELATI</b>",
            "BTC/USDT · ETH/USDT · SOL/USDT · 15m LONG",
            "Periodo studio: <b>09/08/2025 → 09/08/2026 UTC</b> · warm-up dal 01/07/2025",
            "Split fissato prima del test: <b>6 mesi DEV · 3 mesi VALIDATION · 3 mesi TEST</b>",
            f"Costi: fee {100*s['costs']['buy']:.3f}% + {100*s['costs']['sell']:.3f}% · spread {100*s['costs']['spread']:.3f}%",
            "",
        ]
        for phase, label in (("DEV", "🏗 DEV"), ("VAL", "🧪 VALIDATION"), ("TEST", "🔒 TEST FINALE")):
            st = phases[phase]
            lines.append(
                f"{label}: n=<b>{st['n']}</b> · WR <b>{st['wr']:.1f}%</b> · PF <b>{st['pf']:.3f}</b> · "
                f"Exp <b>{100*st['exp']:+.4f}%</b> · P&L €25/trade <b>€{st['net_eur']:+.2f}</b>"
            )
        lines += [
            "",
            f"📊 <b>INTERO ANNO</b>: n={full['n']} · WR {full['wr']:.1f}% · PF {full['pf']:.3f} · Exp {100*full['exp']:+.4f}% · P&L €{full['net_eur']:+.2f}",
            f"Exp lorda media {100*full['gross_exp']:+.4f}% · dopo sole fee {100*full['fee_exp']:+.4f}% · dopo fee+spread {100*full['exp']:+.4f}%",
            f"Bootstrap 95% TEST: <b>{100*tci[0]:+.4f}% → {100*tci[1]:+.4f}%</b>",
            f"Bootstrap 95% anno: {100*fci[0]:+.4f}% → {100*fci[1]:+.4f}%",
            f"Mesi profittevoli: <b>{s['positive_months']}/{len(s['monthly'])}</b> · concentrazione top pair {100*full['top_pair_share']:.1f}%",
            "",
            "🪙 <b>Per coppia · intero anno</b>",
        ]
        for pair, block in s["per_pair"].items():
            st = block["ALL"]
            tst = block["TEST"]
            lines.append(f"• {pair}: n={st['n']} · PF {st['pf']:.2f} · €{st['net_eur']:+.2f} · TEST n={tst['n']} PF {tst['pf']:.2f}")
        cov = s.get("coverage", [])
        if cov:
            lines.append("")
            lines.append("📥 <b>Dati Binance</b>")
            for x in cov:
                lines.append(f"• {x['pair']}: {x['bars']} barre · gap stimati {x['missing_intervals']} · {x['requests']} richieste")
        lines += [
            "",
            "Benchmark storico dichiarato: OOS n=41 · WR 68.29% · PF 1.041 · Exp +0.0280%/trade.",
        ]
        verdict = s.get("verdict")
        if verdict == "EDGE_CONFIRMED":
            lines.append("✅ <b>EDGE CONFERMATO</b>: CAND000006 supera anche il TEST finale congelato con i costi attuali.")
        elif verdict == "SAMPLE_INSUFFICIENT":
            lines.append("⚠️ <b>CAMPIONE INSUFFICIENTE</b>: 12 mesi non producono abbastanza trade per il gate predefinito.")
        else:
            lines.append("❌ <b>EDGE NON CONFERMATO</b>: la configurazione congelata non supera i criteri predefiniti sul vero storico Binance.")
        lines.append("Il TEST non viene usato per ritoccare RSI, breakout, ROI, stop o altri parametri.")
        return "\n".join(lines)
