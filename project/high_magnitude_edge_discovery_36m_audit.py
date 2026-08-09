"""High-magnitude cross-sectional edge discovery on Binance Spot.

This study deliberately stops searching for tiny indicator edges.  It asks a
harder economic question: can a predeclared market event generate enough gross
expectancy to leave a useful margin after the current ~37 bps round-trip cost?

Protocol (frozen before results):
- data: Binance Spot 1h archives, 2023-07-01 -> 2026-08-09 UTC;
- benchmark: BTC/USDT;
- traded research universe: ETH/SOL/BNB/XRP/DOGE USDT;
- timeframes: 1h, 2h, 4h;
- 18 predeclared event families based on BTC lead/lag, relative strength,
  ratio breakouts, volume/range shocks and cross-sectional dispersion/breadth;
- three predeclared wide exit profiles; only DEV may choose one profile per
  event x timeframe family;
- chronological validation: DEV -> VAL -> HOLDOUT -> EXTERNAL_A -> EXTERNAL_B;
- no later phase may change event, timeframe, TP, SL or holding period.

The DEV gate intentionally requires gross expectancy >= 70 bps/trade, not just
statistical positivity.  SHORT results are theoretical on Spot candles.
"""
from __future__ import annotations

from typing import Any
import io
import json
import math
import random
import statistics
import zipfile

import pandas as pd
import requests

from project.cryptoresearch_v1_cand000006_binance_12m_v2_audit import ARCHIVE_BASE
from project.path_edge_discovery_binance_36m_audit import (
    DEV_START,
    DEV_END,
    VAL_END,
    HOLDOUT_END,
    EXTERNAL_A_END,
    EXTERNAL_B_END,
)

AUDIT_VERSION = "HIGH_MAGNITUDE_EDGE_DISCOVERY_36M_20260809_V1_FROZEN"
INTERVAL = "1h"
FETCH_START = pd.Timestamp("2023-07-01T00:00:00Z")

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT")
PAIR_NAMES = {
    "BTCUSDT": "BTC/USDT",
    "ETHUSDT": "ETH/USDT",
    "SOLUSDT": "SOL/USDT",
    "BNBUSDT": "BNB/USDT",
    "XRPUSDT": "XRP/USDT",
    "DOGEUSDT": "DOGE/USDT",
}
BENCHMARK_PAIR = "BTC/USDT"
TRADED_PAIRS = tuple(PAIR_NAMES[s] for s in SYMBOLS if s != "BTCUSDT")

TIMEFRAMES: dict[str, tuple[str, int]] = {
    "1h": ("1h", 60),
    "2h": ("2h", 120),
    "4h": ("4h", 240),
}

EXIT_PROFILES: tuple[tuple[str, float, float, int], ...] = (
    ("WIDE_A", 0.025, 0.015, 24),
    ("WIDE_B", 0.035, 0.020, 36),
    ("WIDE_C", 0.050, 0.025, 48),
)

MIN_DEV = 25
MIN_VAL = 15
MIN_HOLDOUT = 15
MIN_EXTERNAL = 25
DEV_MIN_GROSS = 0.0070
DEV_MIN_NET = 0.0025
DEV_MIN_PF = 1.20
MID_MIN_GROSS = 0.0050
EXT_A_MIN_GROSS = 0.0045
FINAL_MIN_GROSS = 0.0050
FINAL_MIN_NET = 0.0015
FINAL_MIN_PF = 1.15

PHASE_BOUNDS = {
    "DEV": (DEV_START, DEV_END),
    "VAL": (DEV_END, VAL_END),
    "HOLDOUT": (VAL_END, HOLDOUT_END),
    "EXTERNAL_A": (HOLDOUT_END, EXTERNAL_A_END),
    "EXTERNAL_B": (EXTERNAL_A_END, EXTERNAL_B_END),
}


class HighMagnitudeEdgeDiscovery36mAudit:
    def __init__(
        self,
        postgres: object,
        logger: object,
        pairs: list[str] | tuple[str, ...],
        *,
        exchange: str,
        pullback_bars: int,
        max_bars_per_pair: int,
        notional_eur: float,
        buy_fee_rate: float,
        sell_fee_rate: float,
        spread_rate: float,
        slippage_rate: float,
    ) -> None:
        self.postgres = postgres
        self.logger = logger
        self.notional = float(notional_eur)
        self.buy_fee = float(buy_fee_rate)
        self.sell_fee = float(sell_fee_rate)
        self.spread = float(spread_rate)
        self.slippage = float(slippage_rate)

    def _ensure_marker(self) -> None:
        self.postgres.execute(
            """CREATE TABLE IF NOT EXISTS statistics.velez_candidate_audit_runs(
                   version TEXT PRIMARY KEY,
                   created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                   summary JSONB NOT NULL DEFAULT '{}'::jsonb
               )"""
        )

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

    @staticmethod
    def _read_zip(payload: bytes) -> pd.DataFrame:
        columns = [
            "timestamp", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
        ]
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            if not names:
                raise RuntimeError("BINANCE_ARCHIVE_EMPTY")
            with archive.open(names[0]) as handle:
                frame = pd.read_csv(handle, header=None, names=columns)
        raw_ts = pd.to_numeric(frame["timestamp"], errors="coerce")
        median = float(raw_ts.dropna().median()) if raw_ts.notna().any() else 0.0
        unit = "us" if median > 1.0e14 else "ms"
        frame["timestamp"] = pd.to_datetime(raw_ts, unit=unit, utc=True, errors="coerce")
        for col in ("open", "high", "low", "close", "volume"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        return frame[["timestamp", "open", "high", "low", "close", "volume"]].dropna()

    def _fetch_symbol(self, symbol: str) -> tuple[pd.DataFrame, dict[str, Any]]:
        session = requests.Session()
        session.headers.update({"User-Agent": "high-magnitude-edge-discovery/1.0"})
        frames: list[pd.DataFrame] = []
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
        if len(frame) < 26000:
            raise RuntimeError(f"BINANCE_36M_1H_HISTORY_TOO_SHORT symbol={symbol} bars={len(frame)}")
        diffs = frame["timestamp"].diff().dropna().dt.total_seconds().div(3600.0)
        missing = int(sum(max(0, int(round(v)) - 1) for v in diffs if v > 1.0))
        return frame, {
            "symbol": symbol,
            "pair": PAIR_NAMES[symbol],
            "bars": len(frame),
            "from": str(frame.iloc[0]["timestamp"]),
            "to": str(frame.iloc[-1]["timestamp"]),
            "requests": len(urls),
            "missing_intervals": missing,
            "transport": "BINANCE_VISION_1H_ARCHIVES",
        }

    @staticmethod
    def _resample(raw: pd.DataFrame, tf: str) -> pd.DataFrame:
        rule, minutes = TIMEFRAMES[tf]
        if tf == "1h":
            x = raw[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        else:
            x = raw.set_index("timestamp").resample(
                rule, origin="epoch", label="left", closed="left"
            ).agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum",
            }).dropna().reset_index()
        x = x[(x["timestamp"] + pd.Timedelta(minutes=minutes)) <= EXTERNAL_B_END]
        return x.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)

    @staticmethod
    def _base_features(raw: pd.DataFrame, tf: str) -> pd.DataFrame:
        x = HighMagnitudeEdgeDiscovery36mAudit._resample(raw, tf)
        prev_close = x["close"].shift(1)
        tr = pd.concat([
            x["high"] - x["low"],
            (x["high"] - prev_close).abs(),
            (x["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr20 = tr.rolling(20).mean()
        rng = (x["high"] - x["low"]).replace(0.0, math.nan)
        bars_4h = max(1, int(round(240 / TIMEFRAMES[tf][1])))
        x["ret_tf"] = x["close"] / prev_close - 1.0
        x["ret4"] = x["close"] / x["close"].shift(bars_4h) - 1.0
        x["rvol20"] = x["volume"] / x["volume"].rolling(20).mean().replace(0.0, math.nan)
        x["range_atr"] = tr / atr20.replace(0.0, math.nan)
        x["close_loc"] = (x["close"] - x["low"]) / rng
        return x

    def _prepare_all(
        self, raw_by_pair: dict[str, pd.DataFrame]
    ) -> dict[tuple[str, str], tuple[pd.DataFrame, dict[str, tuple[pd.Series, str]]]]:
        base: dict[tuple[str, str], pd.DataFrame] = {}
        for pair, raw in raw_by_pair.items():
            for tf in TIMEFRAMES:
                base[(pair, tf)] = self._base_features(raw, tf)

        prepared: dict[tuple[str, str], tuple[pd.DataFrame, dict[str, tuple[pd.Series, str]]]] = {}
        for tf in TIMEFRAMES:
            btc = base[(BENCHMARK_PAIR, tf)][["timestamp", "close", "ret_tf", "ret4"]].rename(columns={
                "close": "btc_close", "ret_tf": "btc_ret_tf", "ret4": "btc_ret4",
            })
            rs_matrix = pd.concat(
                [base[(pair, tf)].set_index("timestamp")["ret4"].rename(pair) for pair in TRADED_PAIRS],
                axis=1,
                join="inner",
            )
            cs = pd.DataFrame(index=rs_matrix.index)
            cs["cs_max"] = rs_matrix.max(axis=1)
            cs["cs_min"] = rs_matrix.min(axis=1)
            cs["cs_disp"] = cs["cs_max"] - cs["cs_min"]
            cs["breadth_up"] = (rs_matrix >= 0.010).sum(axis=1)
            cs["breadth_down"] = (rs_matrix <= -0.010).sum(axis=1)
            cs = cs.reset_index()

            for pair in TRADED_PAIRS:
                x = base[(pair, tf)].merge(btc, on="timestamp", how="inner").merge(cs, on="timestamp", how="inner")
                x["rs4"] = x["ret4"] - x["btc_ret4"]
                x["ratio"] = x["close"] / x["btc_close"].replace(0.0, math.nan)
                x["ratio_hi20"] = x["ratio"].rolling(20).max().shift(1)
                x["ratio_lo20"] = x["ratio"].rolling(20).min().shift(1)
                top = x["ret4"] >= (x["cs_max"] - 1e-12)
                bottom = x["ret4"] <= (x["cs_min"] + 1e-12)
                strong_up = (x["rvol20"] >= 2.0) & (x["range_atr"] >= 1.5) & (x["close_loc"] >= 0.75)
                strong_down = (x["rvol20"] >= 2.0) & (x["range_atr"] >= 1.5) & (x["close_loc"] <= 0.25)

                events: dict[str, tuple[pd.Series, str]] = {
                    "BTC_LEAD_UP_LAG": ((x["btc_ret4"] >= 0.030) & (x["ret4"] <= x["btc_ret4"] - 0.020) & (x["ret4"] <= 0.015), "LONG"),
                    "BTC_LEAD_DOWN_LAG": ((x["btc_ret4"] <= -0.030) & (x["ret4"] >= x["btc_ret4"] + 0.020) & (x["ret4"] >= -0.015), "SHORT"),
                    "BTC_SHOCK_UP_LAG": ((x["btc_ret_tf"] >= 0.015) & (x["ret_tf"] <= 0.005) & (x["ret_tf"] >= -0.010), "LONG"),
                    "BTC_SHOCK_DOWN_LAG": ((x["btc_ret_tf"] <= -0.015) & (x["ret_tf"] >= -0.005) & (x["ret_tf"] <= 0.010), "SHORT"),
                    "RS_EXTREME_DOWN_REVERT": ((x["rs4"] <= -0.030) & (x["btc_ret4"].abs() <= 0.060), "LONG"),
                    "RS_EXTREME_UP_REVERT": ((x["rs4"] >= 0.030) & (x["btc_ret4"].abs() <= 0.060), "SHORT"),
                    "RS_MOM_UP": ((x["rs4"] >= 0.030) & strong_up, "LONG"),
                    "RS_MOM_DOWN": ((x["rs4"] <= -0.030) & strong_down, "SHORT"),
                    "RATIO_BREAK20_UP": ((x["ratio"] > x["ratio_hi20"]) & (x["rvol20"] >= 1.20), "LONG"),
                    "RATIO_BREAK20_DOWN": ((x["ratio"] < x["ratio_lo20"]) & (x["rvol20"] >= 1.20), "SHORT"),
                    "VOL_RANGE_RS_UP": (strong_up & (x["rs4"] >= 0.015), "LONG"),
                    "VOL_RANGE_RS_DOWN": (strong_down & (x["rs4"] <= -0.015), "SHORT"),
                    "CS_TOP_MOM": (top & (x["cs_disp"] >= 0.040), "LONG"),
                    "CS_BOTTOM_MOM": (bottom & (x["cs_disp"] >= 0.040), "SHORT"),
                    "CS_TOP_REVERT": (top & (x["cs_disp"] >= 0.040), "SHORT"),
                    "CS_BOTTOM_REVERT": (bottom & (x["cs_disp"] >= 0.040), "LONG"),
                    "BREADTH_UP_LAG": ((x["breadth_up"] >= 4) & (x["ret4"] <= 0.005), "LONG"),
                    "BREADTH_DOWN_LAG": ((x["breadth_down"] >= 4) & (x["ret4"] >= -0.005), "SHORT"),
                }
                prepared[(pair, tf)] = (x.reset_index(drop=True), events)
        return prepared

    def _net_return(self, entry: float, exit_px: float, direction: str) -> tuple[float, float, float]:
        q = exit_px / entry
        h = self.spread / 2.0 + self.slippage
        if direction == "LONG":
            gross = q - 1.0
            fee_only = gross - self.buy_fee - q * self.sell_fee
        else:
            gross = 1.0 - q
            fee_only = gross - self.sell_fee - q * self.buy_fee
        economic = fee_only - (1.0 + q) * h
        return gross, fee_only, economic

    def _simulate_pair_phase(
        self,
        x: pd.DataFrame,
        mask: pd.Series,
        direction: str,
        pair: str,
        tf: str,
        event_name: str,
        tp_pct: float,
        sl_pct: float,
        hold_h: int,
        phase: str,
    ) -> list[dict[str, Any]]:
        start, end = PHASE_BOUNDS[phase]
        next_ts = x["timestamp"].shift(-1)
        active = mask.fillna(False).astype(bool) & (next_ts >= start) & (next_ts < end)
        signal_indices = x.index[active].to_numpy(dtype=int, copy=False)
        if len(signal_indices) == 0:
            return []

        opens = x["open"].to_numpy(dtype=float, copy=False)
        highs = x["high"].to_numpy(dtype=float, copy=False)
        lows = x["low"].to_numpy(dtype=float, copy=False)
        closes = x["close"].to_numpy(dtype=float, copy=False)
        timestamps = x["timestamp"].to_numpy(copy=False)
        max_bars = max(1, int(math.ceil(hold_h * 60 / TIMEFRAMES[tf][1])))
        nrows = len(x)
        blocked_until = -1
        trades: list[dict[str, Any]] = []

        for signal_raw in signal_indices:
            signal_i = int(signal_raw)
            entry_i = signal_i + 1
            if entry_i <= blocked_until or entry_i >= nrows:
                continue
            entry_time = pd.Timestamp(timestamps[entry_i])
            entry = float(opens[entry_i])
            if not math.isfinite(entry) or entry <= 0:
                continue
            last_i = min(nrows - 1, entry_i + max_bars - 1)
            if pd.Timestamp(timestamps[last_i]) >= end:
                continue

            if direction == "LONG":
                target, stop = entry * (1.0 + tp_pct), entry * (1.0 - sl_pct)
            else:
                target, stop = entry * (1.0 - tp_pct), entry * (1.0 + sl_pct)

            exit_px: float | None = None
            exit_i = last_i
            reason = "TIMEOUT"
            for k in range(entry_i, last_i + 1):
                op, hi, lo = float(opens[k]), float(highs[k]), float(lows[k])
                if direction == "LONG":
                    if op <= stop:
                        exit_px, exit_i, reason = op, k, "STOP_GAP"; break
                    if op >= target:
                        exit_px, exit_i, reason = op, k, "TARGET_GAP"; break
                    if lo <= stop:
                        exit_px, exit_i, reason = stop, k, "STOP"; break
                    if hi >= target:
                        exit_px, exit_i, reason = target, k, "TARGET"; break
                else:
                    if op >= stop:
                        exit_px, exit_i, reason = op, k, "STOP_GAP"; break
                    if op <= target:
                        exit_px, exit_i, reason = op, k, "TARGET_GAP"; break
                    if hi >= stop:
                        exit_px, exit_i, reason = stop, k, "STOP"; break
                    if lo <= target:
                        exit_px, exit_i, reason = target, k, "TARGET"; break
            if exit_px is None:
                exit_px = float(closes[last_i])
            gross, fee_only, net = self._net_return(entry, float(exit_px), direction)
            trades.append({
                "pair": pair, "tf": tf, "event": event_name, "direction": direction,
                "entry_time": entry_time, "exit_time": pd.Timestamp(timestamps[exit_i]),
                "phase": phase, "gross_r": gross, "fee_r": fee_only, "net_r": net,
                "net_eur": self.notional * net, "reason": reason,
            })
            blocked_until = exit_i
        return trades

    @staticmethod
    def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
        gross = [float(r["gross_r"]) for r in rows]
        net = [float(r["net_r"]) for r in rows]
        wins = [v for v in net if v > 0.0]
        losses = [v for v in net if v < 0.0]
        gp, gl = sum(wins), abs(sum(losses))
        pair_pnl = {pair: 0.0 for pair in TRADED_PAIRS}
        for r in rows:
            pair_pnl[str(r["pair"])] += float(r["net_eur"])
        positive_values = [v for v in pair_pnl.values() if v > 0.0]
        positive_total = sum(positive_values)
        top = max(positive_values, default=0.0)
        return {
            "n": len(rows),
            "wr": 100.0 * len(wins) / len(rows) if rows else 0.0,
            "pf": gp / gl if gl else (999.0 if gp else 0.0),
            "gross_exp": statistics.mean(gross) if gross else 0.0,
            "exp": statistics.mean(net) if net else 0.0,
            "net_eur": sum(float(r["net_eur"]) for r in rows),
            "positive_pair_frac": sum(1 for v in pair_pnl.values() if v > 0.0) / len(TRADED_PAIRS),
            "top_pair_share": top / positive_total if positive_total > 0.0 else 1.0,
            "pair_pnl": pair_pnl,
        }

    @staticmethod
    def _bootstrap(rows: list[dict[str, Any]], seed: int, nboot: int = 3000) -> tuple[float, float]:
        vals = [float(r["net_r"]) for r in rows]
        if len(vals) < 10:
            return (0.0, 0.0)
        rng = random.Random(seed)
        n = len(vals)
        means = [sum(vals[rng.randrange(n)] for _ in range(n)) / n for _ in range(nboot)]
        means.sort()
        return means[int(0.025 * nboot)], means[min(nboot - 1, int(0.975 * nboot))]

    def _rows_for(
        self,
        prepared: dict[tuple[str, str], tuple[pd.DataFrame, dict[str, tuple[pd.Series, str]]]],
        tf: str,
        event_name: str,
        profile: tuple[str, float, float, int],
        phase: str,
    ) -> tuple[list[dict[str, Any]], str]:
        profile_name, tp, sl, hold_h = profile
        rows: list[dict[str, Any]] = []
        direction = "LONG"
        for pair in TRADED_PAIRS:
            x, events = prepared[(pair, tf)]
            mask, direction = events[event_name]
            rows.extend(self._simulate_pair_phase(
                x, mask, direction, pair, tf, event_name, tp, sl, hold_h, phase
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

        prepared = self._prepare_all(raw_by_pair)
        event_names = list(prepared[(TRADED_PAIRS[0], "1h")][1].keys())
        families: list[dict[str, Any]] = []
        dev_pass_n = val_pass_n = hold_pass_n = ext_a_pass_n = 0
        confirmed: list[dict[str, Any]] = []

        for tf in TIMEFRAMES:
            for event_name in event_names:
                best: dict[str, Any] | None = None
                for profile in EXIT_PROFILES:
                    dev_rows, direction = self._rows_for(prepared, tf, event_name, profile, "DEV")
                    dev = self._stats(dev_rows)
                    if dev["n"] < MIN_DEV:
                        continue
                    candidate = {
                        "id": f"{tf}_{event_name}_{profile[0]}",
                        "tf": tf, "event": event_name, "direction": direction,
                        "profile": profile[0], "tp": profile[1], "sl": profile[2], "hold_h": profile[3],
                        "dev": dev, "dev_rows": dev_rows,
                    }
                    if best is None or (dev["gross_exp"], dev["exp"], dev["pf"]) > (
                        best["dev"]["gross_exp"], best["dev"]["exp"], best["dev"]["pf"]
                    ):
                        best = candidate
                if best is None:
                    continue

                dev_ci = self._bootstrap(best["dev_rows"], seed=510000 + len(families))
                best["dev_ci"] = dev_ci
                d = best["dev"]
                best["dev_pass"] = bool(
                    d["n"] >= MIN_DEV
                    and d["gross_exp"] >= DEV_MIN_GROSS
                    and d["exp"] >= DEV_MIN_NET
                    and d["pf"] >= DEV_MIN_PF
                    and dev_ci[0] > 0.0
                    and d["positive_pair_frac"] >= 0.40
                )
                if best["dev_pass"]:
                    dev_pass_n += 1

                profile = (best["profile"], best["tp"], best["sl"], best["hold_h"])
                phase_stats: dict[str, dict[str, Any]] = {}
                phase_rows: dict[str, list[dict[str, Any]]] = {}
                for phase in ("VAL", "HOLDOUT", "EXTERNAL_A", "EXTERNAL_B"):
                    rows, _ = self._rows_for(prepared, tf, event_name, profile, phase)
                    phase_rows[phase] = rows
                    phase_stats[phase] = self._stats(rows)
                best.update({
                    "val": phase_stats["VAL"],
                    "holdout": phase_stats["HOLDOUT"],
                    "external_a": phase_stats["EXTERNAL_A"],
                    "external_b": phase_stats["EXTERNAL_B"],
                })

                v = best["val"]
                best["val_pass"] = bool(
                    best["dev_pass"] and v["n"] >= MIN_VAL and v["gross_exp"] >= MID_MIN_GROSS
                    and v["exp"] > 0.0 and v["pf"] > 1.05
                )
                if best["val_pass"]:
                    val_pass_n += 1

                h = best["holdout"]
                best["holdout_pass"] = bool(
                    best["val_pass"] and h["n"] >= MIN_HOLDOUT and h["gross_exp"] >= MID_MIN_GROSS
                    and h["exp"] > 0.0 and h["pf"] > 1.05
                )
                if best["holdout_pass"]:
                    hold_pass_n += 1

                a = best["external_a"]
                best["external_a_pass"] = bool(
                    best["holdout_pass"] and a["n"] >= MIN_EXTERNAL and a["gross_exp"] >= EXT_A_MIN_GROSS
                    and a["exp"] > 0.0 and a["pf"] > 1.05 and a["positive_pair_frac"] >= 0.40
                )
                if best["external_a_pass"]:
                    ext_a_pass_n += 1

                b = best["external_b"]
                b_ci = self._bootstrap(phase_rows["EXTERNAL_B"], seed=610000 + len(families)) if best["external_a_pass"] else (0.0, 0.0)
                best["external_b_ci"] = b_ci
                best["confirmed"] = bool(
                    best["external_a_pass"] and b["n"] >= MIN_EXTERNAL
                    and b["gross_exp"] >= FINAL_MIN_GROSS and b["exp"] >= FINAL_MIN_NET
                    and b["pf"] >= FINAL_MIN_PF and b_ci[0] > 0.0
                    and b["positive_pair_frac"] >= 0.40 and b["top_pair_share"] <= 0.70
                )
                if best["confirmed"]:
                    confirmed.append({k: v for k, v in best.items() if k != "dev_rows"})
                families.append({k: v for k, v in best.items() if k != "dev_rows"})

        top_dev = sorted(
            families,
            key=lambda r: (float(r["dev"]["gross_exp"]), float(r["dev"]["exp"]), float(r["dev"]["pf"])),
            reverse=True,
        )[:10]
        finalists = sorted(
            [r for r in families if r.get("external_a_pass")],
            key=lambda r: (float(r["external_b"]["exp"]), float(r["external_b"]["gross_exp"])),
            reverse=True,
        )[:8]
        summary = {
            "version": AUDIT_VERSION,
            "benchmark": BENCHMARK_PAIR,
            "traded_pairs": list(TRADED_PAIRS),
            "timeframes": list(TIMEFRAMES.keys()),
            "events": len(event_names),
            "families_total": len(event_names) * len(TIMEFRAMES),
            "families_eligible": len(families),
            "dev_configs": len(event_names) * len(TIMEFRAMES) * len(EXIT_PROFILES),
            "exit_profiles": [{"name": n, "tp": tp, "sl": sl, "hold_h": h} for n, tp, sl, h in EXIT_PROFILES],
            "dev_pass": dev_pass_n,
            "val_pass": val_pass_n,
            "holdout_pass": hold_pass_n,
            "external_a_pass": ext_a_pass_n,
            "confirmed": confirmed,
            "top_dev": top_dev,
            "finalists": finalists,
            "coverage": coverage,
            "costs": {"buy": self.buy_fee, "sell": self.sell_fee, "spread": self.spread, "slippage": self.slippage},
        }
        self._save_marker(summary)
        return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        if s.get("skipped"):
            return ""
        c = s["costs"]
        lines = [
            "🔭 <b>HIGH-MAGNITUDE EDGE DISCOVERY · 36 MESI</b>",
            "Benchmark BTC/USDT · trade ETH/SOL/BNB/XRP/DOGE · Binance Spot candles 1h",
            "TF segnale: 1h / 2h / 4h · eventi cross-sectional/lead-lag/volume/dispersione",
            f"Eventi: <b>{s['events']}</b> · famiglie: <b>{s['families_total']}</b> · configurazioni DEV: <b>{s['dev_configs']}</b>",
            "Exit predefinite: 2.5/1.5% 24h · 3.5/2.0% 36h · 5.0/2.5% 48h",
            f"Costi reali modello: fee {100*c['buy']:.3f}% + {100*c['sell']:.3f}% · spread {100*c['spread']:.3f}%",
            "Gate DEV: lordo medio ≥0.70% · netto ≥0.25% · PF ≥1.20 · bootstrap netto inferiore >0",
            "DEV 08/2023→02/2024 · VAL 02→05 · HOLDOUT 05→08/2024 · A 08/2024→08/2025 · B 08/2025→08/2026",
            "",
            "🏗 <b>Top DEV per magnitudine lorda</b>",
        ]
        for r in s.get("top_dev", [])[:8]:
            d, ci = r["dev"], r.get("dev_ci", (0.0, 0.0))
            lines.append(
                f"• {r['tf']} · {r['event']} · {r['direction']} · TP {100*r['tp']:.1f}% / SL {100*r['sl']:.1f}% / {r['hold_h']}h: "
                f"n={d['n']} gross {100*d['gross_exp']:+.3f}% · net {100*d['exp']:+.3f}% · PF {d['pf']:.2f} · CI net {100*ci[0]:+.3f}%→{100*ci[1]:+.3f}%"
            )
        lines += [
            "",
            "🧪 <b>Funnel economico + replicazione</b>",
            f"Famiglie predefinite: <b>{s['families_total']}</b> · con campione DEV sufficiente: <b>{s['families_eligible']}</b>",
            f"Superano il gate DEV ad alta magnitudine: <b>{s['dev_pass']}</b>",
            f"Passano anche VALIDATION: <b>{s['val_pass']}</b>",
            f"Passano anche HOLDOUT: <b>{s['holdout_pass']}</b>",
            f"Passano anche EXTERNAL A: <b>{s['external_a_pass']}</b>",
            f"Confermati su EXTERNAL B: <b>{len(s.get('confirmed', []))}</b>",
        ]
        if s.get("finalists"):
            lines += ["", "🔒 <b>Arrivati alla replica finale</b>"]
            for r in s["finalists"][:6]:
                a, b, ci = r["external_a"], r["external_b"], r.get("external_b_ci", (0.0, 0.0))
                lines.append(
                    f"• {r['tf']} · {r['event']} · {r['direction']} · {r['profile']} · "
                    f"A n={a['n']} gross {100*a['gross_exp']:+.3f}% net {100*a['exp']:+.3f}% PF {a['pf']:.2f} · "
                    f"B n={b['n']} gross {100*b['gross_exp']:+.3f}% net {100*b['exp']:+.3f}% PF {b['pf']:.2f} CI {100*ci[0]:+.3f}%→{100*ci[1]:+.3f}%"
                )
        lines += ["", "📥 <b>Dati</b>"]
        for row in s.get("coverage", []):
            lines.append(f"• {row['pair']}: {row['bars']} barre 1h · gap {row['missing_intervals']} · richieste {row['requests']}")
        if s.get("confirmed"):
            lines += [
                "",
                "✅ <b>EDGE AD ALTA MAGNITUDINE REPLICATO</b>: almeno una famiglia supera costi e fasi esterne.",
                "Prima di qualunque live servono portfolio audit, slippage stress e PAPER dedicato.",
            ]
        elif s.get("dev_pass", 0) == 0:
            lines += [
                "",
                "❌ <b>NESSUN EVENTO SUPERA GIÀ IL GATE ECONOMICO DEV</b>.",
                "Le famiglie predefinite non producono ≥0.70% lordo medio con robustezza sufficiente nel periodo di sviluppo.",
            ]
        else:
            lines += [
                "",
                "❌ <b>NESSUN EDGE AD ALTA MAGNITUDINE REPLICATO</b> fino alla fase finale.",
                "I candidati forti nel DEV non mantengono il vantaggio lungo il percorso cronologico.",
            ]
        lines.append("⚠️ SHORT è teorico su Spot; short reale richiede margin/futures.")
        return "\n".join(lines)
