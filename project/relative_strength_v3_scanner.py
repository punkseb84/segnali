"""Balanced relative-strength scanner with aligned data and auditable filters.

Uses BTC-relative ranking only for candidate selection. Entries require either:
1) pullback + restart, or 2) controlled breakout continuation.
Every cycle records a complete audit for the whole ranked universe.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Any

import pandas as pd

from project.relative_strength_scanner import RelativeStrengthScanner
from project.strategy_engine.service import GeneratedSignal

RUNTIME_VERSION_V3 = "RELATIVE_STRENGTH_BALANCED_1H_15M_V4"
STRATEGY_VERSION_V3 = "RELATIVE_STRENGTH_BALANCED_V4"


class RelativeStrengthV3Scanner(RelativeStrengthScanner):
    def __init__(
        self,
        *args: Any,
        max_extension_atr: float = 1.25,
        pullback_lookback: int = 8,
        breakout_lookback: int = 8,
        btc_regime_tolerance: float = 0.008,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.max_extension_atr = max(0.50, float(max_extension_atr))
        self.pullback_lookback = max(4, int(pullback_lookback))
        self.breakout_lookback = max(4, int(breakout_lookback))
        self.btc_regime_tolerance = max(0.0, float(btc_regime_tolerance))
        self.last_rejections: Counter[str] = Counter()
        self.last_ranked: list[dict[str, Any]] = []
        self.last_audit: list[dict[str, Any]] = []
        self._active_audit: dict[str, Any] | None = None

    @staticmethod
    def _aligned(asset: pd.DataFrame, btc: pd.DataFrame) -> pd.DataFrame:
        left = asset[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        right = btc[["timestamp", "close"]].rename(columns={"close": "btc_close"}).copy()
        return (
            pd.merge(left, right, on="timestamp", how="inner")
            .dropna()
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

    @staticmethod
    def _pct(series: pd.Series, periods: int) -> float:
        if len(series) <= periods:
            return 0.0
        before = float(series.iloc[-1 - periods])
        return 0.0 if before == 0 else float(series.iloc[-1] / before - 1.0)

    def _rank_pair(
        self,
        pair: str,
        btc_15m: pd.DataFrame,
        btc_1h: pd.DataFrame,
    ) -> dict[str, Any] | None:
        asset_15m = self._frame(pair, "15m", 180)
        asset_1h = self._frame(pair, "1h", 140)
        if len(asset_15m) < 50 or len(asset_1h) < 35:
            return None
        aligned_15 = self._aligned(asset_15m, btc_15m)
        aligned_1h = self._aligned(asset_1h, btc_1h)
        if len(aligned_15) < 40 or len(aligned_1h) < 25:
            return None

        indicators = self._indicators(asset_15m)
        latest = indicators.iloc[-1]
        volume_ratio = float(latest["volume_ratio"])
        if pd.isna(volume_ratio):
            volume_ratio = 0.0

        relative_15m = self._pct(aligned_15["close"], 1) - self._pct(aligned_15["btc_close"], 1)
        relative_1h = self._pct(aligned_15["close"], 4) - self._pct(aligned_15["btc_close"], 4)
        relative_4h = self._pct(aligned_1h["close"], 4) - self._pct(aligned_1h["btc_close"], 4)
        ratio = aligned_15["close"] / aligned_15["btc_close"]
        ratio_fast = ratio.ewm(span=5, adjust=False).mean()
        ratio_slow = ratio.ewm(span=20, adjust=False).mean()
        ratio_momentum = float(ratio_fast.iloc[-1] / ratio_slow.iloc[-1] - 1.0)

        btc_close = aligned_1h["btc_close"]
        btc_ema20 = btc_close.ewm(span=20, adjust=False).mean()
        btc_4h_return = self._pct(btc_close, 4)
        btc_bullish = bool(
            btc_close.iloc[-1] >= btc_ema20.iloc[-1]
            and btc_4h_return >= -self.btc_regime_tolerance
        )
        btc_bearish = bool(
            btc_close.iloc[-1] <= btc_ema20.iloc[-1]
            and btc_4h_return <= self.btc_regime_tolerance
        )

        rs_score = (
            0.50 * relative_4h * 100.0
            + 0.30 * relative_1h * 100.0
            + 0.15 * ratio_momentum * 100.0
            + 0.05 * relative_15m * 100.0
        )
        return {
            "pair": pair,
            "asset_15m": indicators,
            "rs_score": rs_score,
            "relative_4h": relative_4h,
            "relative_1h": relative_1h,
            "relative_15m": relative_15m,
            "ratio_momentum": ratio_momentum,
            "volume_ratio": volume_ratio,
            "btc_bullish": btc_bullish,
            "btc_bearish": btc_bearish,
            "btc_4h_return": btc_4h_return,
        }

    def _reject(self, reason: str):
        self.last_rejections[reason] += 1
        if self._active_audit is not None:
            self._active_audit["decision"] = "REJECTED"
            self._active_audit["reason"] = reason
        return None

    def _build_candidate(
        self,
        item: dict[str, Any],
        direction: str,
        rank: int,
        universe_size: int,
    ):
        frame = item["asset_15m"]
        audit = {
            "pair": str(item["pair"]),
            "direction": direction,
            "rank": rank,
            "universe_size": universe_size,
            "rs_score": round(float(item["rs_score"]), 4),
            "relative_4h_pct": round(float(item["relative_4h"]) * 100.0, 3),
            "relative_1h_pct": round(float(item["relative_1h"]) * 100.0, 3),
            "volume_ratio": round(float(item.get("volume_ratio") or 0.0), 3),
            "btc_4h_pct": round(float(item.get("btc_4h_return") or 0.0) * 100.0, 3),
            "decision": "EVALUATING",
            "reason": "",
            "setup": "NONE",
        }
        self.last_audit.append(audit)
        self._active_audit = audit
        try:
            if len(frame) < max(self.pullback_lookback, self.breakout_lookback) + 3:
                return self._reject("INSUFFICIENT_HISTORY")
            latest, previous = frame.iloc[-1], frame.iloc[-2]
            recent = frame.iloc[-1 - self.pullback_lookback : -1]
            breakout_window = frame.iloc[-1 - self.breakout_lookback : -1]
            entry = float(latest["close"])
            atr = float(latest["atr"])
            ema20 = float(latest["ema20"])
            previous_ema20 = float(previous["ema20"])
            volume_ratio = float(item.get("volume_ratio") or 0.0)
            audit.update({"entry": entry, "atr": atr, "ema20": ema20})
            if atr <= 0:
                return self._reject("ATR_INVALID")
            if volume_ratio < self.volume_ratio_min:
                return self._reject("VOLUME_LOW")

            extension_atr = abs(entry - ema20) / atr
            audit["extension_atr"] = round(extension_atr, 3)
            if extension_atr > self.max_extension_atr:
                return self._reject("OVEREXTENDED")

            if direction == "LONG":
                if not bool(item.get("btc_bullish")):
                    return self._reject("BTC_REGIME")
                pullback = bool(
                    (recent["low"] <= recent["ema20"] + 0.30 * recent["atr"]).any()
                )
                restart = bool(
                    entry > float(latest["open"])
                    and entry > ema20
                    and entry > float(previous["high"])
                    and ema20 >= previous_ema20
                )
                breakout = bool(
                    entry > float(breakout_window["high"].max())
                    and entry > ema20
                    and volume_ratio >= max(0.90, self.volume_ratio_min)
                )
            else:
                if not bool(item.get("btc_bearish")):
                    return self._reject("BTC_REGIME")
                pullback = bool(
                    (recent["high"] >= recent["ema20"] - 0.30 * recent["atr"]).any()
                )
                restart = bool(
                    entry < float(latest["open"])
                    and entry < ema20
                    and entry < float(previous["low"])
                    and ema20 <= previous_ema20
                )
                breakout = bool(
                    entry < float(breakout_window["low"].min())
                    and entry < ema20
                    and volume_ratio >= max(0.90, self.volume_ratio_min)
                )

            audit.update({"pullback": pullback, "restart": restart, "breakout": breakout})
            setup = (
                "PULLBACK_RESTART"
                if pullback and restart
                else "CONTROLLED_BREAKOUT"
                if breakout
                else None
            )
            if setup is None:
                return self._reject("NO_ENTRY_TRIGGER")

            candidate = RelativeStrengthScanner._build_candidate(
                self, item, direction, rank, universe_size
            )
            if candidate is None:
                return self._reject("ECONOMICS_OR_BASE_FILTER")
            audit.update(
                {
                    "decision": "QUALIFIED",
                    "reason": "PASSED_ALL_FILTERS",
                    "setup": setup,
                    "net_rr": round(float(candidate.net_rr), 3),
                    "net_profit_eur": round(float(candidate.net_profit_tp1_eur), 4),
                    "net_loss_eur": round(float(candidate.net_loss_sl_eur), 4),
                }
            )
            candidate.score_breakdown.update(
                {
                    "runtime_version": RUNTIME_VERSION_V3,
                    "strategy_version": STRATEGY_VERSION_V3,
                    "setup_type": setup,
                    "btc_regime_confirmed": True,
                    "btc_4h_return": round(float(item.get("btc_4h_return") or 0.0), 8),
                    "extension_atr": round(extension_atr, 4),
                    "timestamps_aligned": True,
                }
            )
            candidate.reasons.extend(
                [
                    "candele asset/BTC allineate per timestamp",
                    "regime BTC coerente con la direzione",
                    f"setup {setup}",
                    f"estensione {extension_atr:.2f} ATR",
                ]
            )
            return candidate
        finally:
            self._active_audit = None

    def _log_full_audit(self, ranked: list[dict[str, Any]], published: list[GeneratedSignal]) -> None:
        audited_pairs = {str(row["pair"]) for row in self.last_audit}
        selected_pairs = {
            str(item["pair"])
            for item in ranked[: self.strongest_count]
            + list(reversed(ranked[-self.weakest_count :]))
        }
        for rank, item in enumerate(ranked, start=1):
            pair = str(item["pair"])
            if pair not in audited_pairs:
                self.last_audit.append(
                    {
                        "pair": pair,
                        "direction": "WATCHLIST",
                        "rank": rank,
                        "universe_size": len(ranked),
                        "rs_score": round(float(item["rs_score"]), 4),
                        "relative_4h_pct": round(float(item["relative_4h"]) * 100.0, 3),
                        "relative_1h_pct": round(float(item["relative_1h"]) * 100.0, 3),
                        "volume_ratio": round(float(item.get("volume_ratio") or 0.0), 3),
                        "decision": "NOT_SELECTED",
                        "reason": "OUTSIDE_TOP_BOTTOM_SELECTION" if pair not in selected_pairs else "NOT_EVALUATED",
                        "setup": "NONE",
                    }
                )

        self.last_audit.sort(key=lambda row: int(row.get("rank") or 999))
        qualified = sum(1 for row in self.last_audit if row.get("decision") == "QUALIFIED")
        published_pairs = [signal.pair for signal in published]
        self.logger.info(
            "RS_V4_AUDIT_SUMMARY universe=%s evaluated=%s qualified=%s published=%s rejections=%s",
            len(ranked),
            sum(1 for row in self.last_audit if row.get("decision") != "NOT_SELECTED"),
            qualified,
            published_pairs,
            dict(self.last_rejections),
        )
        for row in self.last_audit:
            self.logger.info(
                "RS_V4_AUDIT pair=%s rank=%s/%s direction=%s score=%s rs4h=%s%% rs1h=%s%% volume=%sx extension=%s setup=%s decision=%s reason=%s",
                row.get("pair"),
                row.get("rank"),
                row.get("universe_size"),
                row.get("direction"),
                row.get("rs_score"),
                row.get("relative_4h_pct"),
                row.get("relative_1h_pct"),
                row.get("volume_ratio"),
                row.get("extension_atr", "n/a"),
                row.get("setup", "NONE"),
                row.get("decision"),
                row.get("reason"),
            )
        top_five = sorted(
            self.last_audit,
            key=lambda row: abs(float(row.get("rs_score") or 0.0)),
            reverse=True,
        )[:5]
        self.logger.info(
            "RS_V4_TOP5 %s",
            [
                {
                    "pair": row.get("pair"),
                    "rank": row.get("rank"),
                    "score": row.get("rs_score"),
                    "decision": row.get("decision"),
                    "reason": row.get("reason"),
                    "setup": row.get("setup"),
                }
                for row in top_five
            ],
        )

    def evaluate(self) -> GeneratedSignal | None:
        self.last_rejections = Counter()
        self.last_audit = []

        sent_today = self._count(today_only=True)
        open_positions = self._count(open_only=True)
        if sent_today >= self.max_signals_per_day:
            self.logger.info(
                "RS_V4_AUDIT_BLOCK reason=DAILY_SIGNAL_LIMIT sent_today=%s limit=%s",
                sent_today,
                self.max_signals_per_day,
            )
            return None
        if open_positions >= self.max_open_positions:
            self.logger.info(
                "RS_V4_AUDIT_BLOCK reason=MAX_OPEN_POSITIONS open=%s limit=%s",
                open_positions,
                self.max_open_positions,
            )
            return None

        btc_15m = self._frame("BTC/USD", "15m", 180)
        btc_1h = self._frame("BTC/USD", "1h", 140)
        if len(btc_15m) < 40 or len(btc_1h) < 30:
            self.logger.info("RS_V4_AUDIT_BLOCK reason=BTC_DATA_INSUFFICIENT")
            return None

        ranked: list[dict[str, Any]] = []
        for pair in self.pairs:
            if pair == "BTC/USD":
                continue
            try:
                item = self._rank_pair(pair, btc_15m, btc_1h)
            except Exception:
                self.logger.exception("RS_V4 pair_failed pair=%s", pair)
                continue
            if item is not None:
                ranked.append(item)
        ranked.sort(key=lambda value: float(value["rs_score"]), reverse=True)
        self.last_ranked = ranked

        selections: list[tuple[dict[str, Any], str, int]] = []
        for rank, item in enumerate(ranked[: self.strongest_count], start=1):
            if float(item["rs_score"]) >= self.min_abs_score:
                selections.append((item, "LONG", rank))
            else:
                self.last_rejections["RS_SCORE_TOO_LOW"] += 1
                self.last_audit.append(
                    {
                        "pair": item["pair"], "direction": "LONG", "rank": rank,
                        "universe_size": len(ranked), "rs_score": round(float(item["rs_score"]), 4),
                        "relative_4h_pct": round(float(item["relative_4h"]) * 100.0, 3),
                        "relative_1h_pct": round(float(item["relative_1h"]) * 100.0, 3),
                        "volume_ratio": round(float(item.get("volume_ratio") or 0.0), 3),
                        "decision": "REJECTED", "reason": "RS_SCORE_TOO_LOW", "setup": "NONE",
                    }
                )
        weak = list(reversed(ranked[-self.weakest_count :]))
        for weak_rank, item in enumerate(weak, start=1):
            global_rank = len(ranked) - weak_rank + 1
            if float(item["rs_score"]) <= -self.min_abs_score:
                selections.append((item, "SHORT", global_rank))
            else:
                self.last_rejections["RS_SCORE_TOO_LOW"] += 1
                self.last_audit.append(
                    {
                        "pair": item["pair"], "direction": "SHORT", "rank": global_rank,
                        "universe_size": len(ranked), "rs_score": round(float(item["rs_score"]), 4),
                        "relative_4h_pct": round(float(item["relative_4h"]) * 100.0, 3),
                        "relative_1h_pct": round(float(item["relative_1h"]) * 100.0, 3),
                        "volume_ratio": round(float(item.get("volume_ratio") or 0.0), 3),
                        "decision": "REJECTED", "reason": "RS_SCORE_TOO_LOW", "setup": "NONE",
                    }
                )

        candidates: list[GeneratedSignal] = []
        for item, direction, rank in selections:
            candidate = self._build_candidate(item, direction, rank, len(ranked))
            if candidate is not None:
                candidates.append(candidate)
        candidates.sort(
            key=lambda signal: abs(float(signal.score_breakdown["rs_score"])),
            reverse=True,
        )

        limit = min(
            self.max_signals_per_cycle,
            self.max_signals_per_day - sent_today,
            self.max_open_positions - open_positions,
        )
        published: list[GeneratedSignal] = []
        for candidate in candidates:
            if len(published) >= limit:
                break
            if self.find_active_duplicate(candidate) is not None:
                self.last_rejections["DUPLICATE_OR_PAIR_LIMIT"] += 1
                continue
            signal_id = self.save_signal(candidate)
            signal = replace(candidate, signal_id=signal_id)
            self.publish_signal_event(signal)
            published.append(signal)
            self.logger.info(
                "RS_V4_SIGNAL id=%s pair=%s direction=%s score=%.4f rank=%s setup=%s",
                signal_id,
                signal.pair,
                signal.score_breakdown.get("direction"),
                signal.score_breakdown.get("rs_score"),
                signal.score_breakdown.get("rank"),
                signal.score_breakdown.get("setup_type"),
            )

        self._log_full_audit(ranked, published)
        return published[0] if published else None
