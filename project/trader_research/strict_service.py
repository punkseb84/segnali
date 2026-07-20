"""Strict chronological research pipeline built on public trader fills."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import itertools
import math
from statistics import median
from typing import Any

from project.trader_research.reconstruction import reconstruct_position_cycles
from project.trader_research.service import (
    DEFAULT_RESEARCH_WALLET,
    TraderResearchService,
    coin_to_pair,
)


def normalized_performance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda item: item["open_time"])
    returns = [float(item["net_return_pct"]) for item in ordered]
    pnl = [float(item["net_pnl"]) for item in ordered]
    positive_returns = [value for value in returns if value > 0]
    negative_returns = [-value for value in returns if value < 0]
    positive_pnl = [value for value in pnl if value > 0]
    negative_pnl = [-value for value in pnl if value < 0]
    return_pf = (
        sum(positive_returns) / sum(negative_returns)
        if negative_returns
        else 999.0 if positive_returns else 0.0
    )
    money_pf = (
        sum(positive_pnl) / sum(negative_pnl)
        if negative_pnl
        else 999.0 if positive_pnl else 0.0
    )
    running = maximum = 0
    for value in returns:
        running = running + 1 if value < 0 else 0
        maximum = max(maximum, running)
    return {
        "sample": len(ordered),
        "wins": len(positive_returns),
        "losses": len(negative_returns),
        "win_rate": len(positive_returns) / len(ordered) if ordered else 0.0,
        "profit_factor_return": return_pf,
        "profit_factor_money": money_pf,
        "total_net_pnl": sum(pnl),
        "expectancy_net_pnl": sum(pnl) / len(pnl) if pnl else 0.0,
        "expectancy_return_pct": sum(returns) / len(returns) if returns else 0.0,
        "median_return_pct": median(returns) if returns else 0.0,
        "median_duration_minutes": median(
            [float(item["duration_minutes"]) for item in ordered]
        ) if ordered else 0.0,
        "max_loss_streak": maximum,
    }


class StrictTraderResearchService(TraderResearchService):
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
        candidates: list[dict[str, Any]] = []
        for size in (1, 2):
            for columns in itertools.combinations(dimensions, size):
                combinations = sorted(
                    {tuple(item[column] for column in columns) for item in train},
                    key=str,
                )
                for values in combinations:
                    rule = dict(zip(columns, values))
                    train_rows = [
                        item for item in train
                        if all(item[key] == value for key, value in rule.items())
                    ]
                    test_rows = [
                        item for item in test
                        if all(item[key] == value for key, value in rule.items())
                    ]
                    if len(train_rows) < min_train or len(test_rows) < min_test:
                        continue
                    train_metrics = normalized_performance(train_rows)
                    test_metrics = normalized_performance(test_rows)
                    eligible = bool(
                        train_metrics["profit_factor_return"] >= 1.20
                        and test_metrics["profit_factor_return"] >= 1.10
                        and train_metrics["expectancy_return_pct"] > 0
                        and test_metrics["expectancy_return_pct"] > 0
                        and test_metrics["total_net_pnl"] > 0
                        and test_metrics["max_loss_streak"] <= 5
                    )
                    rank_score = (
                        min(float(test_metrics["profit_factor_return"]), 5.0)
                        + min(float(train_metrics["profit_factor_return"]), 5.0) * 0.4
                        + math.log1p(float(test_metrics["sample"])) * 0.25
                    )
                    raw_key = wallet + "|" + repr(sorted(rule.items()))
                    candidates.append(
                        {
                            "playbook_key": hashlib.sha256(raw_key.encode()).hexdigest(),
                            "rule": rule,
                            "train_metrics": train_metrics,
                            "test_metrics": test_metrics,
                            "eligible_long": eligible,
                            "rank_score": rank_score,
                        }
                    )
        candidates.sort(
            key=lambda item: (bool(item["eligible_long"]), float(item["rank_score"])),
            reverse=True,
        )
        return candidates[:50]

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
            if role.lower() in {"missing", "agent"}:
                raise ValueError(
                    "The address is not a usable Hyperliquid account; provide the main account, subaccount or vault address"
                )
            now = datetime.now(timezone.utc)
            end_ms = int(now.timestamp() * 1000)
            start_ms = int((now - timedelta(days=max(1, lookback_days))).timestamp() * 1000)
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
            pair_map = {
                coin: coin_to_pair(coin, available_pairs)
                for coin in {cycle.coin for cycle in cycles}
            }
            valid_pair_map = {coin: pair for coin, pair in pair_map.items() if pair}
            self.repository.save_cycles(cycles, valid_pair_map)

            research_cycles = [
                cycle for cycle in cycles
                if cycle.complete_for_research
                and cycle.close_reason in {"FLAT", "FLIP"}
                and valid_pair_map.get(cycle.coin)
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
                "feature_exchange": self.market_exchange,
                "lookback_days": lookback_days,
                "fills": len(fills),
                "requested_fill_limit_reached": len(fills) >= min(10_000, max_fills),
                "cycles_total": len(cycles),
                "cycles_complete_mappable": len(research_cycles),
                "features": len(extracted),
                "train_features": sum(item["dataset_split"] == "TRAIN" for item in extracted),
                "test_features": sum(item["dataset_split"] == "TEST" for item in extracted),
                "long_metrics": normalized_performance(
                    [item for item in extracted if item["side"] == "LONG"]
                ),
                "short_metrics_descriptive_only": normalized_performance(
                    [item for item in extracted if item["side"] == "SHORT"]
                ),
                "eligible_long_playbooks": sum(
                    bool(item["eligible_long"]) for item in playbooks
                ),
                "top_long_playbooks": [
                    item for item in playbooks if item["eligible_long"]
                ][:10],
                "unmapped_coins": sorted(
                    coin for coin, pair in pair_map.items() if pair is None
                ),
                "limitations": [
                    "Only the public fill window returned by Hyperliquid is analyzed",
                    "Kraken candles are a cross-exchange behavioural proxy",
                    "SHORT history is descriptive and never exported as an operative rule",
                ],
            }
            self.repository.finish_run(
                run_id,
                status="COMPLETED",
                fill_count=len(fills),
                cycle_count=len(cycles),
                feature_count=len(extracted),
                playbook_count=len(playbooks),
                fill_limit_reached=bool(summary["requested_fill_limit_reached"]),
                diagnostics=diagnostics,
                summary=summary,
            )
            self.logger.info(
                "STRICT_TRADER_RESEARCH_COMPLETED wallet=%s role=%s fills=%s cycles=%s features=%s eligible_long=%s",
                wallet,
                role,
                len(fills),
                len(cycles),
                len(extracted),
                summary["eligible_long_playbooks"],
            )
            return summary
        except Exception as exc:
            self.repository.finish_run(run_id, status="FAILED", error=str(exc))
            self.logger.exception("STRICT_TRADER_RESEARCH_FAILED wallet=%s", wallet)
            raise
