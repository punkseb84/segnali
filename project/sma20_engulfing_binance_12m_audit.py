"""Frozen 12-month audit of the user-supplied SMA20 + engulfing setup.

Rules are fixed before reading results:
- Source: Binance Spot BTC/USDT, ETH/USDT, SOL/USDT 15m public klines.
- Study: 2025-08-09 -> 2026-08-09 UTC, warm-up from 2025-07-01.
- Timeframes: 15m, 30m, 1h, 2h, 4h (resampled from complete 15m bars).
- Bullish entry: close > SMA20 and a strict bullish engulfing candle.
- Bearish entry: close < SMA20 and a strict bearish engulfing candle.
- Entry: next bar open.
- Stop: 1% price distance from entry.
- Target: 2% price distance from entry.
- No trailing, no technical exit, one open trade per pair/candidate.
- Same-candle ambiguity is resolved conservatively: stop before target.
- Costs: configured taker fee each side + configured round-trip spread/slippage.

The screenshot says "stop loss 1% of capital" but that does not define a stop
price.  This audit uses the only fully specified trading interpretation: a 1%
price stop.  Risking 1% of account equity is position sizing and can be tested
separately after an edge exists.

Falsifiable selection protocol:
- 15 predeclared candidates = 5 TF x {BOTH, LONG, SHORT}.
- Candidate must pass DEV and VALIDATION before TEST statistics are opened.
- TEST is never used to alter SMA, engulfing definition, stop, target or TF.

SHORT results are theoretical when using Spot candles; naked shorts require
margin/futures in live trading.
"""
from __future__ import annotations

from typing import Any
import json
import math
import random
import statistics

import pandas as pd

from project.cryptoresearch_v1_cand000006_binance_12m_v2_audit import (
    CryptoResearchV1Cand000006Binance12mV2Audit,
)
from project.cryptoresearch_v1_cand000006_binance_12m_audit import (
    SYMBOLS,
    PAIR_NAMES,
    FETCH_START,
    STUDY_START,
    DEV_END,
    VAL_END,
    STUDY_END,
)

AUDIT_VERSION = "SMA20_ENGULFING_1PCT_SL_2PCT_TP_BINANCE_12M_20260809_V1"
TIMEFRAMES: dict[str, str] = {
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
}
SIDES = ("BOTH", "LONG", "SHORT")
STOP_PCT = 0.01
TARGET_PCT = 0.02
SMA_PERIOD = 20
MIN_DEV_TRADES = 30
MIN_VAL_TRADES = 12
MIN_TEST_TRADES = 12


class Sma20EngulfingBinance12mAudit(CryptoResearchV1Cand000006Binance12mV2Audit):
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
    def _phase(ts: pd.Timestamp) -> str | None:
        if ts < STUDY_START or ts >= STUDY_END:
            return None
        if ts < DEV_END:
            return "DEV"
        if ts < VAL_END:
            return "VAL"
        return "TEST"

    @staticmethod
    def _resample(raw: pd.DataFrame, tf: str) -> pd.DataFrame:
        if tf == "15m":
            x = raw[["timestamp", "open", "high", "low", "close", "volume"]].copy()
            return x.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
        rule = TIMEFRAMES[tf]
        x = raw.set_index("timestamp").resample(
            rule,
            origin="epoch",
            label="left",
            closed="left",
        ).agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna().reset_index()
        # The underlying 15m history is complete; still reject a partial final bar.
        minutes = {"30m": 30, "1h": 60, "2h": 120, "4h": 240}[tf]
        x = x[(x["timestamp"] + pd.Timedelta(minutes=minutes)) <= STUDY_END]
        return x.reset_index(drop=True)

    @staticmethod
    def _prepare(raw: pd.DataFrame, tf: str) -> pd.DataFrame:
        x = Sma20EngulfingBinance12mAudit._resample(raw, tf)
        x["sma20"] = x["close"].astype(float).rolling(SMA_PERIOD).mean()
        prev_open = x["open"].shift(1).astype(float)
        prev_close = x["close"].shift(1).astype(float)
        cur_open = x["open"].astype(float)
        cur_close = x["close"].astype(float)

        # Strict body engulfing: previous candle has opposite colour and the
        # current real body fully contains the previous real body.
        x["bull_engulf"] = (
            (prev_close < prev_open)
            & (cur_close > cur_open)
            & (cur_open <= prev_close)
            & (cur_close >= prev_open)
        )
        x["bear_engulf"] = (
            (prev_close > prev_open)
            & (cur_close < cur_open)
            & (cur_open >= prev_close)
            & (cur_close <= prev_open)
        )
        x["long_signal"] = (cur_close > x["sma20"]) & x["bull_engulf"]
        x["short_signal"] = (cur_close < x["sma20"]) & x["bear_engulf"]
        return x

    def _net_return(self, entry: float, exit_px: float, direction: str) -> tuple[float, float, float]:
        q = exit_px / entry
        h = self.spread / 2.0 + self.slippage
        if direction == "LONG":
            gross = q - 1.0
            fee_only = gross - self.buy_fee - q * self.sell_fee
        else:
            gross = 1.0 - q
            # Entry is a sell and cover is a buy.
            fee_only = gross - self.sell_fee - q * self.buy_fee
        economic = fee_only - (1.0 + q) * h
        return gross, fee_only, economic

    def _simulate_trade(
        self,
        x: pd.DataFrame,
        pair: str,
        tf: str,
        signal_i: int,
        direction: str,
    ) -> dict[str, Any] | None:
        entry_i = signal_i + 1
        if entry_i >= len(x):
            return None
        entry_time = pd.Timestamp(x.iloc[entry_i]["timestamp"])
        if entry_time >= STUDY_END:
            return None
        entry = float(x.iloc[entry_i]["open"])
        if not math.isfinite(entry) or entry <= 0:
            return None

        if direction == "LONG":
            stop = entry * (1.0 - STOP_PCT)
            target = entry * (1.0 + TARGET_PCT)
        else:
            stop = entry * (1.0 + STOP_PCT)
            target = entry * (1.0 - TARGET_PCT)

        for k in range(entry_i, len(x)):
            row = x.iloc[k]
            ts = pd.Timestamp(row["timestamp"])
            if ts >= STUDY_END:
                break
            op = float(row["open"])
            hi = float(row["high"])
            lo = float(row["low"])

            if direction == "LONG":
                if op <= stop:
                    exit_px, reason = op, "STOP_GAP"
                elif op >= target:
                    exit_px, reason = op, "TARGET_GAP"
                elif lo <= stop:
                    exit_px, reason = stop, "STOP"
                elif hi >= target:
                    exit_px, reason = target, "TARGET"
                else:
                    continue
            else:
                if op >= stop:
                    exit_px, reason = op, "STOP_GAP"
                elif op <= target:
                    exit_px, reason = op, "TARGET_GAP"
                elif hi >= stop:
                    exit_px, reason = stop, "STOP"
                elif lo <= target:
                    exit_px, reason = target, "TARGET"
                else:
                    continue

            gross, fee_only, net = self._net_return(entry, float(exit_px), direction)
            return {
                "pair": pair,
                "tf": tf,
                "direction": direction,
                "entry_time": entry_time,
                "exit_time": ts,
                "phase": self._phase(entry_time),
                "entry": entry,
                "exit": float(exit_px),
                "gross_r": gross,
                "fee_r": fee_only,
                "net_r": net,
                "net_eur": self.notional * net,
                "reason": reason,
                "bars": k - entry_i + 1,
            }
        return None

    def _simulate_pair_tf(self, raw: pd.DataFrame, pair: str, tf: str) -> list[dict[str, Any]]:
        x = self._prepare(raw, tf)
        trades: list[dict[str, Any]] = []
        # Separate position state per direction is NOT allowed. One candidate on
        # one pair can hold at most one trade at a time, matching normal strategy use.
        i = 1
        while i < len(x) - 1:
            ts = pd.Timestamp(x.iloc[i]["timestamp"])
            if ts < STUDY_START:
                i += 1
                continue
            if ts >= STUDY_END:
                break
            direction: str | None = None
            if bool(x.iloc[i]["long_signal"]):
                direction = "LONG"
            elif bool(x.iloc[i]["short_signal"]):
                direction = "SHORT"
            if direction is None:
                i += 1
                continue
            trade = self._simulate_trade(x, pair, tf, i, direction)
            if trade is None:
                break
            trades.append(trade)
            exit_ts = pd.Timestamp(trade["exit_time"])
            while i < len(x) - 1 and pd.Timestamp(x.iloc[i]["timestamp"]) <= exit_ts:
                i += 1
        return trades

    @staticmethod
    def _stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
        net = [float(t["net_r"]) for t in trades]
        gross = [float(t["gross_r"]) for t in trades]
        fee = [float(t["fee_r"]) for t in trades]
        wins = [v for v in net if v > 0]
        losses = [v for v in net if v < 0]
        gp = sum(wins)
        gl = abs(sum(losses))
        pair_pnl: dict[str, float] = {}
        directions: dict[str, int] = {"LONG": 0, "SHORT": 0}
        reasons: dict[str, int] = {}
        eq = peak = dd = 0.0
        for t in sorted(trades, key=lambda z: z["exit_time"]):
            val = float(t["net_r"])
            eq += val
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
            pair_pnl[t["pair"]] = pair_pnl.get(t["pair"], 0.0) + float(t["net_eur"])
            directions[t["direction"]] = directions.get(t["direction"], 0) + 1
            reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
        positive = sum(v for v in pair_pnl.values() if v > 0)
        top = max([v for v in pair_pnl.values() if v > 0], default=0.0)
        return {
            "n": len(trades),
            "wr": 100.0 * len(wins) / len(trades) if trades else 0.0,
            "pf": gp / gl if gl else (999.0 if gp else 0.0),
            "gross_exp": statistics.mean(gross) if gross else 0.0,
            "fee_exp": statistics.mean(fee) if fee else 0.0,
            "exp": statistics.mean(net) if net else 0.0,
            "net_eur": sum(float(t["net_eur"]) for t in trades),
            "max_dd": dd,
            "avg_bars": statistics.mean([int(t["bars"]) for t in trades]) if trades else 0.0,
            "pair_pnl": pair_pnl,
            "top_pair_share": top / positive if positive > 0 else 1.0,
            "directions": directions,
            "reasons": reasons,
        }

    @staticmethod
    def _bootstrap(trades: list[dict[str, Any]], seed: int, nboot: int = 4000) -> tuple[float, float]:
        vals = [float(t["net_r"]) for t in trades]
        if len(vals) < 10:
            return (0.0, 0.0)
        rng = random.Random(seed)
        n = len(vals)
        means: list[float] = []
        for _ in range(nboot):
            means.append(sum(vals[rng.randrange(n)] for _ in range(n)) / n)
        means.sort()
        return means[int(0.025 * nboot)], means[min(nboot - 1, int(0.975 * nboot))]

    @staticmethod
    def _candidate_trades(
        trades_by_tf: dict[str, list[dict[str, Any]]],
        tf: str,
        side: str,
        phase: str,
    ) -> list[dict[str, Any]]:
        base = [t for t in trades_by_tf[tf] if t["phase"] == phase]
        if side == "LONG":
            return [t for t in base if t["direction"] == "LONG"]
        if side == "SHORT":
            return [t for t in base if t["direction"] == "SHORT"]
        return base

    @staticmethod
    def _passes(stats: dict[str, Any], min_n: int) -> bool:
        return bool(
            int(stats.get("n", 0)) >= min_n
            and float(stats.get("pf", 0.0)) > 1.0
            and float(stats.get("exp", 0.0)) > 0.0
        )

    def run(self) -> dict[str, Any]:
        if self.already_done():
            return {"skipped": True, "version": AUDIT_VERSION}

        coverage: list[dict[str, Any]] = []
        raw_by_pair: dict[str, pd.DataFrame] = {}
        for symbol in SYMBOLS:
            raw, info = self._fetch_symbol(symbol)
            raw_by_pair[PAIR_NAMES[symbol]] = raw
            coverage.append(info)

        trades_by_tf: dict[str, list[dict[str, Any]]] = {tf: [] for tf in TIMEFRAMES}
        for tf in TIMEFRAMES:
            for pair, raw in raw_by_pair.items():
                trades_by_tf[tf].extend(self._simulate_pair_tf(raw, pair, tf))

        candidates: list[dict[str, Any]] = []
        validated: list[dict[str, Any]] = []
        for tf in TIMEFRAMES:
            for side in SIDES:
                dev_trades = self._candidate_trades(trades_by_tf, tf, side, "DEV")
                val_trades = self._candidate_trades(trades_by_tf, tf, side, "VAL")
                dev = self._stats(dev_trades)
                val = self._stats(val_trades)
                row = {
                    "id": f"{tf}_{side}",
                    "tf": tf,
                    "side": side,
                    "dev": dev,
                    "val": val,
                    "dev_pass": self._passes(dev, MIN_DEV_TRADES),
                    "val_pass": self._passes(val, MIN_VAL_TRADES),
                }
                row["validated"] = bool(row["dev_pass"] and row["val_pass"])
                candidates.append(row)
                if row["validated"]:
                    validated.append(row)

        # Rank only on DEV+VAL. TEST has not been inspected at this point.
        validated.sort(
            key=lambda r: (
                min(float(r["dev"]["pf"]), float(r["val"]["pf"])),
                min(float(r["dev"]["exp"]), float(r["val"]["exp"])),
                int(r["dev"]["n"]) + int(r["val"]["n"]),
            ),
            reverse=True,
        )

        test_results: list[dict[str, Any]] = []
        for rank, row in enumerate(validated, start=1):
            test_trades = self._candidate_trades(trades_by_tf, row["tf"], row["side"], "TEST")
            test = self._stats(test_trades)
            ci = self._bootstrap(test_trades, seed=20260809 + rank)
            confirmed = bool(
                test["n"] >= MIN_TEST_TRADES
                and test["pf"] > 1.10
                and test["exp"] > 0
                and ci[0] > 0
            )
            test_results.append({
                "id": row["id"],
                "tf": row["tf"],
                "side": row["side"],
                "dev": row["dev"],
                "val": row["val"],
                "test": test,
                "test_ci": ci,
                "confirmed": confirmed,
            })

        # Diagnostics for the exact screenshot strategy (BOTH) over the full year.
        full_both: dict[str, Any] = {}
        for tf in TIMEFRAMES:
            full = [t for t in trades_by_tf[tf] if t["phase"] in {"DEV", "VAL", "TEST"}]
            full_both[tf] = self._stats(full)

        confirmed = [r for r in test_results if r["confirmed"]]
        verdict = "EDGE_CONFIRMED" if confirmed else "NO_EDGE_CONFIRMED"
        summary = {
            "version": AUDIT_VERSION,
            "strategy": "SMA20 + bullish/bearish engulfing + SL1% TP2%",
            "study": {
                "fetch_start": str(FETCH_START),
                "study_start": str(STUDY_START),
                "dev_end": str(DEV_END),
                "val_end": str(VAL_END),
                "study_end": str(STUDY_END),
            },
            "timeframes": list(TIMEFRAMES),
            "symbols": [PAIR_NAMES[s] for s in SYMBOLS],
            "notional": self.notional,
            "costs": {
                "buy_fee": self.buy_fee,
                "sell_fee": self.sell_fee,
                "spread": self.spread,
                "slippage": self.slippage,
            },
            "candidates": candidates,
            "validated_count": len(validated),
            "test_results": test_results,
            "confirmed_count": len(confirmed),
            "full_both": full_both,
            "coverage": coverage,
            "verdict": verdict,
            "short_note": "SHORT is theoretical on Spot candles; live naked shorts require margin/futures.",
            "stop_interpretation": "1% price stop, not 1% equity risk sizing.",
        }
        self._save_marker(summary)
        return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        if s.get("skipped"):
            return ""
        costs = s["costs"]
        lines = [
            "🕯 <b>SMA20 + ENGULFING · TEST 12 MESI</b>",
            "BTC/USDT · ETH/USDT · SOL/USDT · Binance Spot candles",
            "Regole: sopra SMA20 + bullish engulfing → LONG · sotto SMA20 + bearish engulfing → SHORT",
            "Entry open barra successiva · SL 1% · TP 2% · stop prima del target se entrambi nella stessa barra",
            f"Costi: fee {100*costs['buy_fee']:.3f}% + {100*costs['sell_fee']:.3f}% · spread {100*costs['spread']:.3f}%",
            "TF predefiniti: 15m / 30m / 1h / 2h / 4h · 15 candidati (BOTH/LONG/SHORT)",
            "Split: 6 mesi DEV · 3 mesi VALIDATION · 3 mesi TEST",
            "",
            "📊 <b>Strategia esatta LONG+SHORT · intero anno (diagnostica)</b>",
        ]
        for tf in s["timeframes"]:
            st = s["full_both"][tf]
            lines.append(
                f"• {tf}: n={st['n']} · WR {st['wr']:.1f}% · PF {st['pf']:.2f} · "
                f"Exp {100*st['exp']:+.3f}% · lordo {100*st['gross_exp']:+.3f}%"
            )

        # Show the strongest DEV candidates even when none qualifies.
        ranked = sorted(
            s["candidates"],
            key=lambda r: (float(r["dev"]["pf"]), float(r["dev"]["exp"]), int(r["dev"]["n"])),
            reverse=True,
        )[:5]
        lines += ["", "🏗 <b>Top DEV</b>"]
        for r in ranked:
            d, v = r["dev"], r["val"]
            lines.append(
                f"• {r['id']}: DEV n={d['n']} PF {d['pf']:.2f} Exp {100*d['exp']:+.3f}% · "
                f"VAL n={v['n']} PF {v['pf']:.2f} Exp {100*v['exp']:+.3f}%"
            )

        lines.append("")
        if not s["test_results"]:
            lines.append(
                f"❌ <b>NESSUN CANDIDATO PASSA DEV + VALIDATION</b> · TEST non aperto."
            )
        else:
            lines.append(f"🔒 <b>TEST aperto per {len(s['test_results'])} candidati pre-validati</b>")
            for r in s["test_results"]:
                t = r["test"]
                ci = r["test_ci"]
                icon = "✅" if r["confirmed"] else "❌"
                lines.append(
                    f"{icon} {r['id']}: n={t['n']} · WR {t['wr']:.1f}% · PF {t['pf']:.2f} · "
                    f"Exp {100*t['exp']:+.3f}% · CI95 {100*ci[0]:+.3f}%→{100*ci[1]:+.3f}%"
                )

        lines += [
            "",
            "⚠️ SHORT è teorico sui dati Spot: per aprire short reali servono margin/futures.",
            "Nota: 'SL 1%' è interpretato come distanza prezzo; rischiare 1% del capitale è sizing, non livello stop.",
        ]
        if s["verdict"] == "EDGE_CONFIRMED":
            lines.append("✅ <b>EDGE CONFERMATO NEL TEST</b> per almeno un candidato pre-validato.")
        else:
            lines.append("❌ <b>EDGE NON CONFERMATO</b> con le regole congelate e i costi attuali.")
        return "\n".join(lines)
