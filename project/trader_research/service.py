"""End-to-end research service for a public Hyperliquid trader wallet."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import itertools
import math
from statistics import median
from typing import Any

import pandas as pd

from indicators import add_indicators, classify_market_regime
from project.shared.logging import get_module_logger
from project.trader_research.hyperliquid import HyperliquidInfoClient
from project.trader_research.reconstruction import PositionCycle, reconstruct_position_cycles
from project.trader_research.repository import TraderResearchRepository


DEFAULT_RESEARCH_WALLET = "0xca4903c207a0df58bf37d4bc8bbc5210726f53c8"


def coin_to_pair(coin: str, available_pairs: set[str]) -> str | None:
    normalized = str(coin or "").strip().upper()
    if not normalized or ":" in normalized or normalized.startswith("@"):  # HIP-3/spot index
        return None
    aliases = {
        "UBTC": "BTC",
        "WETH": "ETH",
        "1000PEPE": "PEPE",
        "KPEPE": "PEPE",
        "1000BONK": "BONK",
        "KBONK": "BONK",
    }
    base = aliases.get(normalized, normalized)
    candidates = [f"{base}/USD", f"{base}/USDT", f"{base}/USDC"]
    return next((pair for pair in candidates if pair in available_pairs), None)


def _frame(rows: list[tuple[Any, ...]]) -> pd.DataFrame:
    data = pd.DataFrame(
        list(reversed(rows)),
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    if data.empty:
        return data
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True)
    for column in ["open", "high", "low", "close", "volume"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return data.dropna().reset_index(drop=True)


def performance_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda item: item["open_time"])
    pnl = [float(item["net_pnl"]) for item in ordered]
    wins = [value for value in pnl if value > 0]
    losses = [-value for value in pnl if value < 0]
    gross_profit, gross_loss = sum(wins), sum(losses)
    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else 999.0
        if gross_profit > 0
        else 0.0
    )
    loss_streak = max_streak = 0
    for value in pnl:
        loss_streak = loss_streak + 1 if value < 0 else 0
        max_streak = max(max_streak, loss_streak)
    return {
        "sample": len(ordered),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(ordered) if ordered else 0.0,
        "profit_factor": profit_factor,
        "total_net_pnl": sum(pnl),
        "expectancy_net_pnl": sum(pnl) / len(pnl) if pnl else 0.0,
        "median_return_pct": median([float(item["net_return_pct"]) for item in ordered]) if ordered else 0.0,
        "median_duration_minutes": median([float(item["duration_minutes"]) for item in ordered]) if ordered else 0.0,
        "max_loss_streak": max_streak,
    }


class TraderResearchService:
    def __init__(
        self,
        repository: TraderResearchRepository,
        api: HyperliquidInfoClient,
        *,
        market_exchange: str = "kraken",
    ) -> None:
        self.repository = repository
        self.api = api
        self.market_exchange = market_exchange
        self.logger = get_module_logger("trader_research")

    def extract_entry_features(
        self,
        cycle: PositionCycle,
        pair: str,
    ) -> dict[str, Any] | None:
        rows_15m = self.repository.fetch_closed_ohlc(
            pair, "15m", cycle.open_time, exchange=self.market_exchange, limit=260
        )
        rows_1h = self.repository.fetch_closed_ohlc(
            pair, "1h", cycle.open_time, exchange=self.market_exchange, limit=260
        )
        frame_15m, frame_1h = _frame(rows_15m), _frame(rows_1h)
        if len(frame_15m) < 220 or len(frame_1h) < 220:
            return None
        frame_15m, frame_1h = add_indicators(frame_15m), add_indicators(frame_1h)
        row, previous = frame_15m.iloc[-1], frame_15m.iloc[-2]
        context = frame_1h.iloc[-1]
        if pd.isna(row.get("atr14")) or float(row["atr14"]) <= 0:
            return None

        close, atr = float(row["close"]), float(row["atr14"])
        ema20, ema50, ema200 = float(row["ema20"]), float(row["ema50"]), float(row["ema200"])
        rsi_value = float(row["rsi14"])
        volume_ratio = float(row["relative_volume"]) if not pd.isna(row["relative_volume"]) else 0.0
        distance_atr = (close - ema20) / atr
        resistance_before = float(frame_15m["high"].iloc[-21:-1].max())
        support_before = float(frame_15m["low"].iloc[-21:-1].min())
        hour = cycle.open_time.hour

        rsi_bucket = (
            "LT40" if rsi_value < 40 else "40_50" if rsi_value < 50 else
            "50_60" if rsi_value < 60 else "60_70" if rsi_value < 70 else "GE70"
        )
        location_bucket = (
            "BELOW_EMA20" if distance_atr < -0.25 else
            "EMA20_PULLBACK" if abs(distance_atr) <= 0.35 else
            "CONTINUATION" if distance_atr <= 1.20 else "EXTENDED"
        )
        volume_bucket = (
            "LOW" if volume_ratio < 0.8 else "NORMAL" if volume_ratio < 1.2 else
            "HIGH" if volume_ratio < 2.0 else "EXTREME"
        )
        session = "ASIA" if hour < 7 else "EUROPE" if hour < 13 else "US" if hour < 21 else "LATE"
        trend_1h = bool(
            float(context["close"]) > float(context["ema200"])
            and float(context["ema20"]) > float(context["ema50"])
        )
        ema_alignment = (
            "BULL" if ema20 > ema50 > ema200 else
            "BEAR" if ema20 < ema50 < ema200 else "MIXED"
        )

        return {
            "cycle_key": cycle.cycle_key,
            "wallet": cycle.wallet,
            "coin": cycle.coin,
            "pair": pair,
            "side": cycle.side,
            "open_time": cycle.open_time,
            "close_time": cycle.close_time,
            "net_pnl": float(cycle.net_pnl),
            "net_return_pct": cycle.net_return_pct,
            "won": cycle.net_pnl > 0,
            "duration_minutes": cycle.duration_minutes,
            "maker_fill_ratio": cycle.maker_fill_ratio,
            "maker_majority": cycle.maker_fill_ratio >= 0.50,
            "session": session,
            "weekday": cycle.open_time.strftime("%a").upper(),
            "regime_15m": classify_market_regime(frame_15m),
            "trend_1h": trend_1h,
            "ema_alignment": ema_alignment,
            "rsi14": rsi_value,
            "rsi_bucket": rsi_bucket,
            "adx14": float(row["adx14"]),
            "atr_pct": atr / close * 100.0,
            "relative_volume": volume_ratio,
            "volume_bucket": volume_bucket,
            "distance_ema20_atr": distance_atr,
            "location_bucket": location_bucket,
            "macd_improving": float(row["macd_hist"]) > float(previous["macd_hist"]),
            "breakout20": close > resistance_before,
            "near_support20": (close - support_before) <= atr * 0.50,
            "close_above_ema200": close > ema200,
            "source_candle_15m": frame_15m.iloc[-1]["timestamp"].isoformat(),
            "source_candle_1h": frame_1h.iloc[-1]["timestamp"].isoformat(),
        }

    @staticmethod
    def mine_long_playbooks(
        wallet: str,
        features: list[dict[str, Any]],
        *,
        min_train: int = 20,
        min_test: int = 8,
    ) -> list[dict[str, Any]]:
        long_rows = [item for item in features if item["side"] == "LONG"]
        train = [item for item in long_rows if item["dataset_split"] == "TRAIN"]
        test = [item for item in long_rows if item["dataset_split"] == "TEST"]
        dimensions = [
            "session", "regime_15m", "trend_1h", "ema_alignment", "rsi_bucket",
            "volume_bucket", "location_bucket", "macd_improving", "breakout20",
            "near_support20", "maker_majority",
        ]
        rules: list[dict[str, Any]] = []
        for size in (1, 2):
            for columns in itertools.combinations(dimensions, size):
                values = sorted({tuple(item[column] for column in columns) for item in train}, key=str)
                for combination in values:
                    rule = dict(zip(columns, combination))
                    train_rows = [item for item in train if all(item[key] == value for key, value in rule.items())]
                    test_rows = [item for item in test if all(item[key] == value for key, value in rule.items())]
                    if len(train_rows) < min_train or len(test_rows) < min_test:
                        continue
                    train_metrics = performance_metrics(train_rows)
                    test_metrics = performance_metrics(test_rows)
                    eligible = bool(
                        train_metrics["profit_factor"] >= 1.20
                        and test_metrics["profit_factor"] >= 1.10
                        and train_metrics["expectancy_net_pnl"] > 0
                        and test_metrics["expectancy_net_pnl"] > 0
                        and test_metrics["total_net_pnl"] > 0
                        and test_metrics["max_loss_streak"] <= 5
                    )
                    rank_score = (
                        min(float(test_metrics["profit_factor"]), 5.0)
                        + min(float(train_metrics["profit_factor"]), 5.0) * 0.4
                        + math.log1p(float(test_metrics["sample"])) * 0.25
                    )
                    raw_key = wallet + "|" + repr(sorted(rule.items()))
                    rules.append(
                        {
                            "playbook_key": hashlib.sha256(raw_key.encode()).hexdigest(),
                            "rule": rule,
                            "train_metrics": train_metrics,
                            "test_metrics": test_metrics,
                            "eligible_long": eligible,
                            "rank_score": rank_score,
                        }
                    )
        rules.sort(key=lambda item: (bool(item["eligible_long"]), float(item["rank_score"])), reverse=True)
        return rules[:50]

    def run(
        self,
        wallet: str = DEFAULT_RESEARCH_WALLET,
        *,
        lookback_days: int = 120,
        max_fills: int = 10_000,
    ) -> dict[str, Any]:
        wallet = self.api.validate_address(wallet)
        self.repository.migrate()
        role = self.api.user_role(wallet)
        run_id = self.repository.start_run(wallet, role)
        try:
            if role == "missing":
                raise ValueError("The address is not a Hyperliquid user/subaccount/vault")
            end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            start_ms = int((datetime.now(timezone.utc) - timedelta(days=max(1, lookback_days))).timestamp() * 1000)
            fills = self.api.fetch_user_fills_by_time(
                wallet,
                start_time_ms=start_ms,
                end_time_ms=end_ms,
                aggregate_by_time=True,
                max_fills=max_fills,
            )
            if not fills:
                raise ValueError("No public fills returned for the selected wallet and period")
            self.repository.save_fills(fills)
            cycles, diagnostics = reconstruct_position_cycles(fills)
            available_pairs = self.repository.available_pairs(self.market_exchange)
            pair_map = {coin: coin_to_pair(coin, available_pairs) for coin in {cycle.coin for cycle in cycles}}
            valid_pair_map = {coin: pair for coin, pair in pair_map.items() if pair}
            self.repository.save_cycles(cycles, valid_pair_map)

            research_cycles = [
                cycle for cycle in cycles
                if cycle.complete_for_research and valid_pair_map.get(cycle.coin)
            ]
            extracted: list[dict[str, Any]] = []
            for cycle in research_cycles:
                feature = self.extract_entry_features(cycle, valid_pair_map[cycle.coin])
                if feature is not None:
                    extracted.append(feature)
            extracted.sort(key=lambda item: item["open_time"])
            split_index = max(1, int(len(extracted) * 0.70)) if extracted else 0
            for index, item in enumerate(extracted):
                split = "TRAIN" if index < split_index else "TEST"
                item["dataset_split"] = split
                self.repository.save_cycle_feature(
                    item["cycle_key"], pair=item["pair"], split=split, features=item
                )

            playbooks = self.mine_long_playbooks(wallet, extracted)
            self.repository.replace_playbooks(wallet, playbooks)
            summary = {
                "wallet": wallet,
                "role": role,
                "lookback_days": lookback_days,
                "fills": len(fills),
                "fill_limit_reached": len(fills) >= min(10_000, max_fills),
                "cycles_total": len(cycles),
                "cycles_complete_mappable": len(research_cycles),
                "features": len(extracted),
                "train_features": sum(item["dataset_split"] == "TRAIN" for item in extracted),
                "test_features": sum(item["dataset_split"] == "TEST" for item in extracted),
                "long_metrics": performance_metrics([item for item in extracted if item["side"] == "LONG"]),
                "short_metrics": performance_metrics([item for item in extracted if item["side"] == "SHORT"]),
                "eligible_long_playbooks": sum(bool(item["eligible_long"]) for item in playbooks),
                "top_long_playbooks": [item for item in playbooks if item["eligible_long"]][:10],
                "unmapped_coins": sorted(coin for coin, pair in pair_map.items() if pair is None),
            }
            self.repository.finish_run(
                run_id,
                status="COMPLETED",
                fill_count=len(fills),
                cycle_count=len(cycles),
                feature_count=len(extracted),
                playbook_count=len(playbooks),
                fill_limit_reached=bool(summary["fill_limit_reached"]),
                diagnostics=diagnostics,
                summary=summary,
            )
            self.logger.info(
                "TRADER_RESEARCH_COMPLETED wallet=%s role=%s fills=%s cycles=%s features=%s eligible_long=%s",
                wallet, role, len(fills), len(cycles), len(extracted), summary["eligible_long_playbooks"],
            )
            return summary
        except Exception as exc:
            self.repository.finish_run(run_id, status="FAILED", error=str(exc))
            self.logger.exception("TRADER_RESEARCH_FAILED wallet=%s", wallet)
            raise
