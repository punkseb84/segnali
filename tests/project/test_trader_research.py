from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from project.trader_research.hyperliquid import HyperliquidFill
from project.trader_research.paged_client import PagedHyperliquidInfoClient
from project.trader_research.reconstruction import reconstruct_position_cycles
from project.trader_research.repository import RESEARCH_SCHEMA
from project.trader_research.service import coin_to_pair
from project.trader_research.strict_service import (
    StrictTraderResearchService,
    normalized_performance,
)


WALLET = "0x1111111111111111111111111111111111111111"


def raw_fill(
    *,
    time_ms: int,
    side: str,
    size: str,
    price: str,
    start: str,
    pnl: str = "0",
    fee: str = "0.10",
    tid: str | None = None,
    coin: str = "BTC",
    crossed: bool = False,
):
    return {
        "coin": coin,
        "side": side,
        "sz": size,
        "px": price,
        "startPosition": start,
        "closedPnl": pnl,
        "fee": fee,
        "feeToken": "USDC",
        "time": time_ms,
        "crossed": crossed,
        "hash": f"0x{time_ms}",
        "oid": str(time_ms),
        "tid": tid or str(time_ms),
        "dir": "Open Long" if side == "B" else "Close Long",
    }


def fill(**kwargs) -> HyperliquidFill:
    return HyperliquidFill.from_api(WALLET, raw_fill(**kwargs))


class QueueClient(PagedHyperliquidInfoClient):
    def __init__(self, pages):
        super().__init__(max_retries=1, sleeper=lambda _: None)
        self.pages = list(pages)
        self.payloads = []

    def _post(self, payload):
        self.payloads.append(payload)
        return self.pages.pop(0) if self.pages else []


def test_fill_pagination_reuses_inclusive_timestamp_without_duplicates() -> None:
    first = raw_fill(time_ms=1_000, side="B", size="1", price="100", start="0", tid="a")
    boundary = raw_fill(time_ms=2_000, side="A", size="1", price="101", start="1", pnl="1", tid="b")
    third = raw_fill(time_ms=3_000, side="B", size="1", price="102", start="0", tid="c")
    client = QueueClient([[first, boundary], [boundary, third]])

    fills = client.fetch_user_fills_by_time(
        WALLET,
        start_time_ms=500,
        end_time_ms=4_000,
        max_fills=3,
    )

    assert [item.trade_id for item in fills] == ["a", "b", "c"]
    assert len(client.payloads) == 2
    assert client.payloads[1]["startTime"] == 2_000


def test_reconstructs_scale_in_partial_close_and_flat_exit() -> None:
    fills = [
        fill(time_ms=1_000, side="B", size="2", price="100", start="0", tid="a"),
        fill(time_ms=2_000, side="B", size="1", price="110", start="2", tid="b"),
        fill(time_ms=3_000, side="A", size="1", price="120", start="3", pnl="20", tid="c"),
        fill(time_ms=4_000, side="A", size="2", price="130", start="2", pnl="50", tid="d"),
    ]

    cycles, diagnostics = reconstruct_position_cycles(fills)

    assert diagnostics["cycles_closed"] == 1
    assert len(cycles) == 1
    cycle = cycles[0]
    assert cycle.side == "LONG"
    assert cycle.close_reason == "FLAT"
    assert cycle.entry_quantity == Decimal("3")
    assert cycle.exit_quantity == Decimal("3")
    assert cycle.average_entry_price == Decimal("310") / Decimal("3")
    assert cycle.average_exit_price == Decimal("380") / Decimal("3")
    assert cycle.gross_closed_pnl == Decimal("70")
    assert cycle.fees == Decimal("0.40")
    assert cycle.net_pnl == Decimal("69.60")
    assert cycle.complete_for_research is True


def test_history_starting_inside_open_position_is_marked_truncated() -> None:
    fills = [
        fill(time_ms=1_000, side="A", size="1", price="110", start="2", pnl="10", tid="a"),
        fill(time_ms=2_000, side="A", size="1", price="120", start="1", pnl="20", tid="b"),
    ]
    cycles, diagnostics = reconstruct_position_cycles(fills)
    assert diagnostics["truncated"] == 1
    assert cycles[0].truncated_start is True
    assert cycles[0].complete_for_research is False


def test_normalized_metrics_do_not_let_position_size_define_return_edge() -> None:
    now = datetime.now(timezone.utc)
    rows = [
        {"open_time": now, "net_return_pct": 2.0, "net_pnl": 10.0, "duration_minutes": 10},
        {"open_time": now + timedelta(minutes=1), "net_return_pct": -1.0, "net_pnl": -1000.0, "duration_minutes": 20},
    ]
    metrics = normalized_performance(rows)
    assert metrics["profit_factor_return"] == pytest.approx(2.0)
    assert metrics["expectancy_return_pct"] == pytest.approx(0.5)
    assert metrics["profit_factor_money"] == pytest.approx(0.01)


def feature(index: int, *, split: str, return_pct: float) -> dict:
    return {
        "open_time": datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index),
        "side": "LONG",
        "dataset_split": split,
        "net_return_pct": return_pct,
        "net_pnl": return_pct,
        "duration_minutes": 30,
        "session": "EUROPE",
        "regime_15m": "TREND_STRONG",
        "trend_1h": True,
        "ema_alignment": "BULL",
        "rsi_bucket": "50_60",
        "volume_bucket": "NORMAL",
        "location_bucket": "EMA20_PULLBACK",
        "macd_improving": True,
        "breakout20": False,
        "near_support20": True,
        "maker_majority": True,
    }


def test_playbook_requires_positive_unseen_test_period() -> None:
    train = [feature(i, split="TRAIN", return_pct=2.0 if i % 2 == 0 else -1.0) for i in range(20)]
    losing_test = [feature(20 + i, split="TEST", return_pct=-1.0) for i in range(8)]
    playbooks = StrictTraderResearchService.mine_long_playbooks(WALLET, train + losing_test)
    assert playbooks
    assert not any(item["eligible_long"] for item in playbooks)


def test_playbook_can_pass_only_when_train_and_test_are_positive() -> None:
    train = [feature(i, split="TRAIN", return_pct=2.0 if i % 2 == 0 else -1.0) for i in range(20)]
    test = [feature(20 + i, split="TEST", return_pct=2.0 if i % 2 == 0 else -1.0) for i in range(8)]
    playbooks = StrictTraderResearchService.mine_long_playbooks(WALLET, train + test)
    assert any(item["eligible_long"] for item in playbooks)


def test_coin_mapping_is_limited_to_available_local_markets() -> None:
    available = {"BTC/USD", "ETH/USD", "PEPE/USD"}
    assert coin_to_pair("BTC", available) == "BTC/USD"
    assert coin_to_pair("1000PEPE", available) == "PEPE/USD"
    assert coin_to_pair("HYPE", available) is None
    assert coin_to_pair("xyz:ABC", available) is None


class AgentApi:
    def validate_address(self, wallet):
        return wallet

    def user_role(self, wallet):
        return "agent"


class RunRepository:
    def __init__(self):
        self.finished = None

    def migrate(self):
        return None

    def start_run(self, wallet, role):
        return 7

    def finish_run(self, run_id, **kwargs):
        self.finished = (run_id, kwargs)


def test_agent_address_is_rejected_before_fill_research() -> None:
    repository = RunRepository()
    service = StrictTraderResearchService(repository, AgentApi())
    with pytest.raises(ValueError, match="main account"):
        service.run(WALLET)
    assert repository.finished[1]["status"] == "FAILED"


def test_schema_is_research_only_and_does_not_modify_live_signal_tables() -> None:
    assert "research.public_trader_fills" in RESEARCH_SCHEMA
    assert "research.public_trader_cycles" in RESEARCH_SCHEMA
    assert "research.public_trader_playbooks" in RESEARCH_SCHEMA
    assert "CREATE TABLE IF NOT EXISTS signals.generated_signals" not in RESEARCH_SCHEMA
