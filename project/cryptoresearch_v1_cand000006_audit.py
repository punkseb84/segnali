"""Independent recent-data audit of Freqtrade CryptoResearchV1 / CAND000006.

This is NOT a reconstruction of the missing original immutable OOS artifact.
It freezes the documented CAND000006 logic and applies it to the OHLC currently
stored by the Railway bot.  The original Freqtrade universe was Binance spot
BTC/USDT, ETH/USDT and SOL/USDT; this audit therefore reports a CORE analogue
(BTC/USD, ETH/USD, SOL/USD where available) separately from the full current
20-pair universe.  Entry is executed at the next 15m bar open.

Frozen entry:
- close > prior 20-bar high (current bar excluded)
- 1h close > EMA200 and EMA50 > EMA200
- 15m close > EMA200, EMA20 > EMA50, EMA20 rising
- RSI14 in [48, 68]
- MACD(12,26,9) histogram > 0 and rising
- RVOL20 >= 1.0
- volume > 0

Frozen exit:
- no technical exit (roi_only)
- minimal ROI 2.5% / 1.5% after 240m / 0.5% after 480m / 0% after 720m
- stoploss -3%
- no trailing

ROI and stop thresholds are solved in NET-return space using the current audit
fees/spread.  This is deliberately stricter than a frictionless reproduction.
"""
from __future__ import annotations

from typing import Any
import json
import math
import random
import statistics

import pandas as pd

from project.velez_candidate_audit import VelezCandidateAudit

AUDIT_VERSION = "CRYPTORESEARCH_V1_CAND000006_RECENT_RECHECK_20260809_V1"
CORE_BASES = {"BTC", "ETH", "SOL"}
TIMEFRAME = "15m"
STOPLOSS_NET = -0.03
ROI_SCHEDULE = ((720, 0.0), (480, 0.005), (240, 0.015), (0, 0.025))


class CryptoResearchV1Cand000006Audit(VelezCandidateAudit):
    def __init__(
        self,
        *args: Any,
        notional_eur: float = 25.0,
        buy_fee_rate: float = 0.0016,
        sell_fee_rate: float = 0.0016,
        spread_rate: float = 0.0005,
        slippage_rate: float = 0.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.notional = float(notional_eur)
        self.buy_fee = float(buy_fee_rate)
        self.sell_fee = float(sell_fee_rate)
        self.spread = float(spread_rate)
        self.slippage = float(slippage_rate)

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
    def _ema(values: pd.Series, period: int) -> pd.Series:
        """SMA-seeded EMA, close to TA-Lib EMA semantics."""
        src = pd.to_numeric(values, errors="coerce").astype(float)
        out = pd.Series(math.nan, index=src.index, dtype=float)
        if len(src) < period:
            return out
        first = src.iloc[:period]
        if first.isna().any():
            return out
        prev = float(first.mean())
        out.iloc[period - 1] = prev
        alpha = 2.0 / (period + 1.0)
        for i in range(period, len(src)):
            v = float(src.iloc[i])
            if math.isnan(v):
                continue
            prev = (v - prev) * alpha + prev
            out.iloc[i] = prev
        return out

    @staticmethod
    def _rsi(values: pd.Series, period: int = 14) -> pd.Series:
        src = pd.to_numeric(values, errors="coerce").astype(float)
        out = pd.Series(math.nan, index=src.index, dtype=float)
        if len(src) <= period:
            return out
        delta = src.diff()
        gains = delta.clip(lower=0.0)
        losses = (-delta.clip(upper=0.0))
        avg_gain = float(gains.iloc[1:period + 1].mean())
        avg_loss = float(losses.iloc[1:period + 1].mean())

        def value(g: float, l: float) -> float:
            if l == 0:
                return 100.0 if g > 0 else 0.0
            rs = g / l
            return 100.0 - 100.0 / (1.0 + rs)

        out.iloc[period] = value(avg_gain, avg_loss)
        for i in range(period + 1, len(src)):
            avg_gain = (avg_gain * (period - 1) + float(gains.iloc[i])) / period
            avg_loss = (avg_loss * (period - 1) + float(losses.iloc[i])) / period
            out.iloc[i] = value(avg_gain, avg_loss)
        return out

    @classmethod
    def _indicators_15m(cls, d: pd.DataFrame) -> pd.DataFrame:
        x = d[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        x = x.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
        c = x["close"].astype(float)
        x["ema20"] = cls._ema(c, 20)
        x["ema50"] = cls._ema(c, 50)
        x["ema200"] = cls._ema(c, 200)
        x["rsi"] = cls._rsi(c, 14)
        ema12 = cls._ema(c, 12)
        ema26 = cls._ema(c, 26)
        x["macd"] = ema12 - ema26
        # Seed the signal EMA only on the valid MACD tail.
        valid = x["macd"].dropna()
        sig = pd.Series(math.nan, index=x.index, dtype=float)
        if len(valid) >= 9:
            tail_sig = cls._ema(valid.reset_index(drop=True), 9)
            sig.loc[valid.index] = tail_sig.to_numpy()
        x["macdsignal"] = sig
        x["macdhist"] = x["macd"] - x["macdsignal"]
        x["volume_mean20"] = x["volume"].astype(float).rolling(20).mean()
        x["rvol"] = x["volume"].astype(float) / x["volume_mean20"].replace(0, math.nan)
        x["previous_high20"] = x["high"].astype(float).rolling(20).max().shift(1)
        return x

    @classmethod
    def _attach_safe_1h(cls, d15: pd.DataFrame) -> pd.DataFrame:
        """Attach only completed 1h bars to each 15m row (no informative look-ahead)."""
        h = d15[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        h = h.set_index("timestamp").resample(
            "1h", origin="epoch", label="left", closed="left"
        ).agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna().reset_index()
        h["ema50_1h"] = cls._ema(h["close"].astype(float), 50)
        h["ema200_1h"] = cls._ema(h["close"].astype(float), 200)
        # An informative bar timestamped 10:00 is knowable only from 11:00 onward.
        h["available_at"] = h["timestamp"] + pd.Timedelta(hours=1)
        h = h[["available_at", "close", "ema50_1h", "ema200_1h"]].rename(columns={"close": "close_1h"})
        x = d15.sort_values("timestamp").copy()
        return pd.merge_asof(
            x,
            h.sort_values("available_at"),
            left_on="timestamp",
            right_on="available_at",
            direction="backward",
        )

    @staticmethod
    def _is_entry(x: pd.DataFrame, i: int) -> bool:
        r = x.iloc[i]
        needed = (
            "close", "volume", "previous_high20", "close_1h", "ema50_1h", "ema200_1h",
            "ema20", "ema50", "ema200", "rsi", "macdhist", "rvol",
        )
        if any(pd.isna(r[k]) for k in needed) or i < 1:
            return False
        prev_hist = x.iloc[i - 1]["macdhist"]
        prev_ema20 = x.iloc[i - 1]["ema20"]
        if pd.isna(prev_hist) or pd.isna(prev_ema20):
            return False
        return bool(
            float(r["close"]) > float(r["previous_high20"])
            and float(r["volume"]) > 0.0
            and float(r["close_1h"]) > float(r["ema200_1h"])
            and float(r["ema50_1h"]) > float(r["ema200_1h"])
            and float(r["close"]) > float(r["ema200"])
            and float(r["ema20"]) > float(r["ema50"])
            and float(r["ema20"]) > float(prev_ema20)
            and 48.0 <= float(r["rsi"]) <= 68.0
            and float(r["macdhist"]) > 0.0
            and float(r["macdhist"]) > float(prev_hist)
            and float(r["rvol"]) >= 1.0
        )

    def _net_return(self, entry: float, exit_px: float) -> float:
        if entry <= 0 or exit_px <= 0:
            return 0.0
        q = exit_px / entry
        half_friction = self.spread / 2.0 + self.slippage
        return q - 1.0 - self.buy_fee - q * self.sell_fee - (1.0 + q) * half_friction

    def _price_for_net_return(self, entry: float, target_net: float) -> float:
        """Solve the exit price whose modeled net return equals target_net."""
        h = self.spread / 2.0 + self.slippage
        denom = 1.0 - self.sell_fee - h
        numer = 1.0 + self.buy_fee + h + target_net
        return entry * numer / denom

    @staticmethod
    def _roi_for_minutes(minutes: int) -> float:
        for threshold, roi in ROI_SCHEDULE:
            if minutes >= threshold:
                return roi
        return 0.025

    def _trade(self, x: pd.DataFrame, pair: str, signal_i: int) -> dict[str, Any] | None:
        entry_i = signal_i + 1
        if entry_i >= len(x):
            return None
        entry = float(x.iloc[entry_i]["open"])
        entry_time = x.iloc[entry_i]["timestamp"]
        if entry <= 0:
            return None
        stop_px = self._price_for_net_return(entry, STOPLOSS_NET)

        for k in range(entry_i, len(x)):
            row = x.iloc[k]
            low = float(row["low"])
            high = float(row["high"])
            open_px = float(row["open"])
            elapsed = int((row["timestamp"] - entry_time).total_seconds() // 60)
            roi = self._roi_for_minutes(elapsed)
            roi_px = self._price_for_net_return(entry, roi)

            # Conservative Freqtrade-like ordering: stop before ROI within a candle.
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

            net_r = self._net_return(entry, exit_px)
            return {
                "pair": pair,
                "entry_time": entry_time,
                "exit_time": row["timestamp"],
                "entry": entry,
                "exit": exit_px,
                "net_r": net_r,
                "net_eur": self.notional * net_r,
                "reason": reason,
                "bars": k - entry_i + 1,
            }
        return None

    def _pair_trades(self, pair: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        raw = self._frame(pair)
        if raw.empty:
            return [], {"pair": pair, "bars": 0, "days": 0.0, "signals": 0, "unresolved": 0}
        x = self._attach_safe_1h(self._indicators_15m(raw))
        days = max(0.0, (x.iloc[-1]["timestamp"] - x.iloc[0]["timestamp"]).total_seconds() / 86400.0)
        trades: list[dict[str, Any]] = []
        unresolved = 0
        signals = 0
        i = 1
        while i < len(x) - 1:
            if not self._is_entry(x, i):
                i += 1
                continue
            signals += 1
            t = self._trade(x, pair, i)
            if t is None:
                unresolved += 1
                break
            trades.append(t)
            # one position per pair: skip signals until after the trade closed
            exit_ts = t["exit_time"]
            while i < len(x) - 1 and x.iloc[i]["timestamp"] <= exit_ts:
                i += 1
        return trades, {"pair": pair, "bars": len(x), "days": days, "signals": signals, "unresolved": unresolved}

    @staticmethod
    def _stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
        vals = [float(t["net_r"]) for t in trades]
        wins = [v for v in vals if v > 0]
        losses = [v for v in vals if v < 0]
        gp = sum(wins)
        gl = abs(sum(losses))
        reasons: dict[str, int] = {}
        pair_pnl: dict[str, float] = {}
        for t in trades:
            reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
            pair_pnl[t["pair"]] = pair_pnl.get(t["pair"], 0.0) + float(t["net_eur"])
        return {
            "n": len(trades),
            "wr": 100.0 * len(wins) / len(trades) if trades else 0.0,
            "pf": gp / gl if gl else (999.0 if gp else 0.0),
            "exp_r": statistics.mean(vals) if vals else 0.0,
            "net_r": sum(vals),
            "net_eur": sum(float(t["net_eur"]) for t in trades),
            "avg_bars": statistics.mean([int(t["bars"]) for t in trades]) if trades else 0.0,
            "reasons": reasons,
            "pair_pnl": pair_pnl,
        }

    @staticmethod
    def _bootstrap(trades: list[dict[str, Any]], seed: int = 600006, nboot: int = 3000) -> tuple[float, float]:
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

    @staticmethod
    def _windows(trades: list[dict[str, Any]], parts: int = 3) -> list[dict[str, Any]]:
        z = sorted(trades, key=lambda t: t["entry_time"])
        out = []
        for p in range(parts):
            a = round(len(z) * p / parts)
            b = round(len(z) * (p + 1) / parts)
            out.append(CryptoResearchV1Cand000006Audit._stats(z[a:b]))
        return out

    @staticmethod
    def _core_pair(pair: str) -> bool:
        base = str(pair).split("/")[0].upper()
        return base in CORE_BASES

    def run(self) -> dict[str, Any]:
        if self.already_done():
            return {"skipped": True, "version": AUDIT_VERSION}
        all_trades: list[dict[str, Any]] = []
        coverage: list[dict[str, Any]] = []
        per_pair: dict[str, Any] = {}
        for pair in self.pairs[:20]:
            trades, info = self._pair_trades(pair)
            all_trades.extend(trades)
            coverage.append(info)
            per_pair[pair] = self._stats(trades)

        core = [t for t in all_trades if self._core_pair(t["pair"])]
        all_stats = self._stats(all_trades)
        core_stats = self._stats(core)
        core_ci = self._bootstrap(core)
        all_ci = self._bootstrap(all_trades)
        core_windows = self._windows(core)
        all_windows = self._windows(all_trades)

        # This is a recent robustness audit, not proof/reproduction of the historic OOS.
        if core_stats["n"] < 20:
            verdict = "CORE_SAMPLE_INSUFFICIENT"
        elif core_stats["pf"] > 1.0 and core_stats["exp_r"] > 0 and core_ci[0] > 0:
            verdict = "RECENT_CORE_CONFIRMED"
        else:
            verdict = "RECENT_CORE_NOT_CONFIRMED"

        summary = {
            "version": AUDIT_VERSION,
            "strategy": "CryptoResearchV1 / CAND000006",
            "timeframe": TIMEFRAME,
            "pairs": list(self.pairs[:20]),
            "core_bases": sorted(CORE_BASES),
            "notional": self.notional,
            "costs": {"buy": self.buy_fee, "sell": self.sell_fee, "spread": self.spread, "slippage": self.slippage},
            "execution": "NEXT_BAR_OPEN",
            "core": core_stats,
            "all20": all_stats,
            "core_ci": core_ci,
            "all_ci": all_ci,
            "core_windows": core_windows,
            "all_windows": all_windows,
            "coverage": coverage,
            "per_pair": per_pair,
            "verdict": verdict,
            "historical_claim": {"n": 41, "wr": 68.29, "pf": 1.041, "exp": 0.00028, "provenance_reproduced": False},
            "original_universe_note": "Binance spot BTC/USDT ETH/USDT SOL/USDT; current audit uses available /USD market data and is a robustness recheck, not exact reproduction.",
        }
        self._save_marker(summary)
        return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        if s.get("skipped"):
            return ""
        core = s.get("core", {})
        all20 = s.get("all20", {})
        cci = s.get("core_ci", (0.0, 0.0))
        aci = s.get("all_ci", (0.0, 0.0))
        cov = s.get("coverage", [])
        days = [float(x.get("days", 0.0)) for x in cov if float(x.get("days", 0.0)) > 0]
        unresolved = sum(int(x.get("unresolved", 0)) for x in cov)
        core_pos = sum(1 for w in s.get("core_windows", []) if float(w.get("net_r", 0.0)) > 0)
        all_pos = sum(1 for w in s.get("all_windows", []) if float(w.get("net_r", 0.0)) > 0)
        lines = [
            "🧪 <b>RECHECK · CryptoResearchV1 / CAND000006</b>",
            "15m LONG · breakout20 + trend 1h/15m + RSI + MACD + RVOL",
            "Exit: ROI-only 2.5% → 1.5% → 0.5% → 0% · SL netto -3% · trailing OFF",
            f"Esecuzione: <b>open barra successiva</b> · €{s.get('notional',25):.2f}/trade",
            f"Costi audit: fee {100*s['costs']['buy']:.3f}% + {100*s['costs']['sell']:.3f}% · spread {100*s['costs']['spread']:.3f}%",
            "",
            "🎯 <b>CORE analogo all’universo originale · BTC/ETH/SOL</b>",
            f"Trade chiusi: <b>{core.get('n',0)}</b> · WR <b>{core.get('wr',0):.1f}%</b> · PF <b>{core.get('pf',0):.3f}</b>",
            f"Expectancy netta: <b>{100*core.get('exp_r',0):+.4f}%/trade</b> · P&L €25/trade <b>€{core.get('net_eur',0):+.2f}</b>",
            f"Bootstrap 95% Exp: <b>{100*cci[0]:+.4f}% → {100*cci[1]:+.4f}%</b> · finestre positive {core_pos}/3",
            "",
            "🌐 <b>Generalizzazione · tutte le 20 crypto disponibili</b>",
            f"Trade chiusi: <b>{all20.get('n',0)}</b> · WR <b>{all20.get('wr',0):.1f}%</b> · PF <b>{all20.get('pf',0):.3f}</b>",
            f"Expectancy netta: <b>{100*all20.get('exp_r',0):+.4f}%/trade</b> · P&L €25/trade <b>€{all20.get('net_eur',0):+.2f}</b>",
            f"Bootstrap 95% Exp: <b>{100*aci[0]:+.4f}% → {100*aci[1]:+.4f}%</b> · finestre positive {all_pos}/3",
        ]
        if days:
            lines.append(f"Copertura corrente: min {min(days):.1f}g · mediana {statistics.median(days):.1f}g · max {max(days):.1f}g")
        if unresolved:
            lines.append(f"Trade ancora irrisolti a fine storico: {unresolved}")
        lines += [
            "",
            "📎 <b>Benchmark storico dichiarato</b>",
            "OOS: n=41 · WR 68.29% · PF 1.041 · Exp +0.0280%/trade",
            "Il repository attuale non contiene gli artefatti immutabili necessari a riprodurre autonomamente quel vecchio OOS.",
            "",
        ]
        v = s.get("verdict")
        if v == "RECENT_CORE_CONFIRMED":
            lines.append("✅ <b>EDGE CORE RECENTE CONFERMATO</b> con bootstrap positivo nel campione disponibile.")
        elif v == "CORE_SAMPLE_INSUFFICIENT":
            lines.append("⚠️ <b>CAMPIONE CORE INSUFFICIENTE</b>: il recheck non può confermare né bocciare statisticamente CAND000006.")
        else:
            lines.append("❌ <b>EDGE CORE RECENTE NON CONFERMATO</b>: il campione disponibile non replica un expectancy robustamente positiva.")
        lines.append("Nota: l’originale era Binance spot /USDT su BTC/ETH/SOL; questo è un recheck indipendente sui dati /USD attualmente disponibili, non la riproduzione esatta del vecchio backtest.")
        return "\n".join(lines)
