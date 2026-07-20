"""Reconstruct flat-to-flat position cycles from public Hyperliquid fills."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
from typing import Iterable

from project.trader_research.hyperliquid import HyperliquidFill

EPS = Decimal("0.000000000001")
ZERO = Decimal("0")


def sign(value: Decimal) -> int:
    return 1 if value > EPS else -1 if value < -EPS else 0


@dataclass(frozen=True)
class PositionCycle:
    cycle_key: str
    wallet: str
    coin: str
    side: str
    open_time: datetime
    close_time: datetime
    average_entry_price: Decimal
    average_exit_price: Decimal
    entry_quantity: Decimal
    exit_quantity: Decimal
    max_abs_position: Decimal
    gross_closed_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    fill_count: int
    maker_fill_ratio: float
    duration_minutes: float
    truncated_start: bool
    close_reason: str

    @property
    def complete_for_research(self) -> bool:
        return not self.truncated_start and self.entry_quantity > 0 and self.exit_quantity > 0

    @property
    def entry_notional(self) -> Decimal:
        return self.average_entry_price * self.entry_quantity

    @property
    def net_return_pct(self) -> float:
        return float(self.net_pnl / self.entry_notional * 100) if self.entry_notional > 0 else 0.0


@dataclass
class _OpenCycle:
    wallet: str
    coin: str
    side_sign: int
    open_time: datetime
    position: Decimal
    truncated_start: bool = False
    entry_notional: Decimal = ZERO
    entry_quantity: Decimal = ZERO
    exit_notional: Decimal = ZERO
    exit_quantity: Decimal = ZERO
    max_abs_position: Decimal = ZERO
    gross_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    fill_count: int = 0
    maker_fills: int = 0
    first_key: str = ""
    last_key: str = ""

    def add_fill(self, fill: HyperliquidFill, fraction: Decimal) -> None:
        if fraction <= 0:
            return
        self.fees += fill.fee * fraction
        self.fill_count += 1
        self.maker_fills += int(not fill.crossed)
        self.first_key = self.first_key or fill.fill_key
        self.last_key = fill.fill_key

    def add_entry(self, quantity: Decimal, price: Decimal) -> None:
        self.entry_quantity += quantity
        self.entry_notional += quantity * price

    def add_exit(self, quantity: Decimal, price: Decimal, pnl: Decimal) -> None:
        self.exit_quantity += quantity
        self.exit_notional += quantity * price
        self.gross_pnl += pnl

    def close(self, when: datetime, reason: str) -> PositionCycle:
        avg_entry = self.entry_notional / self.entry_quantity if self.entry_quantity else ZERO
        avg_exit = self.exit_notional / self.exit_quantity if self.exit_quantity else ZERO
        raw_key = "|".join(
            [self.wallet, self.coin, str(self.side_sign), self.open_time.isoformat(), when.isoformat(), self.first_key, self.last_key]
        )
        return PositionCycle(
            cycle_key=hashlib.sha256(raw_key.encode()).hexdigest(),
            wallet=self.wallet,
            coin=self.coin,
            side="LONG" if self.side_sign > 0 else "SHORT",
            open_time=self.open_time,
            close_time=when,
            average_entry_price=avg_entry,
            average_exit_price=avg_exit,
            entry_quantity=self.entry_quantity,
            exit_quantity=self.exit_quantity,
            max_abs_position=self.max_abs_position,
            gross_closed_pnl=self.gross_pnl,
            fees=self.fees,
            net_pnl=self.gross_pnl - self.fees,
            fill_count=self.fill_count,
            maker_fill_ratio=self.maker_fills / self.fill_count if self.fill_count else 0.0,
            duration_minutes=max(0.0, (when - self.open_time).total_seconds() / 60.0),
            truncated_start=self.truncated_start,
            close_reason=reason,
        )


def reconstruct_position_cycles(
    fills: Iterable[HyperliquidFill],
) -> tuple[list[PositionCycle], dict[str, int]]:
    ordered = sorted(
        (fill for fill in fills if fill.is_valid()),
        key=lambda fill: (fill.time_ms, fill.trade_id, fill.order_id, fill.fill_key),
    )
    active: dict[str, _OpenCycle] = {}
    cycles: list[PositionCycle] = []
    stats = {"fills": len(ordered), "cycles_closed": 0, "truncated": 0, "flips": 0, "discontinuities": 0, "open_at_end": 0}

    for fill in ordered:
        coin, start, end, delta = fill.coin, fill.start_position, fill.end_position, fill.signed_size
        start_sign, end_sign, delta_sign = sign(start), sign(end), sign(delta)
        cycle = active.get(coin)

        if cycle is not None and sign(cycle.position) != start_sign:
            stats["discontinuities"] += 1
            cycles.append(cycle.close(fill.time, "DISCONTINUITY"))
            active.pop(coin, None)
            cycle = None

        if cycle is None and start_sign != 0:
            cycle = _OpenCycle(fill.wallet, coin, start_sign, fill.time, start, True, max_abs_position=abs(start))
            active[coin] = cycle
            stats["truncated"] += 1
        elif cycle is None and start_sign == 0 and end_sign != 0:
            cycle = _OpenCycle(fill.wallet, coin, end_sign, fill.time, ZERO)
            active[coin] = cycle

        if cycle is None or delta_sign == 0:
            continue

        if delta_sign == cycle.side_sign:
            cycle.add_entry(abs(delta), fill.price)
            cycle.add_fill(fill, Decimal("1"))
            cycle.position = end
            cycle.max_abs_position = max(cycle.max_abs_position, abs(end))
            continue

        close_qty = min(abs(start), abs(delta))
        open_qty = max(ZERO, abs(delta) - close_qty)
        close_fraction = close_qty / abs(delta) if delta else ZERO
        cycle.add_exit(close_qty, fill.price, fill.closed_pnl)
        cycle.add_fill(fill, close_fraction)
        closes_cycle = end_sign == 0 or end_sign != cycle.side_sign

        if closes_cycle:
            cycles.append(cycle.close(fill.time, "FLIP" if end_sign else "FLAT"))
            stats["cycles_closed"] += 1
            active.pop(coin, None)

        if open_qty > 0 and end_sign != 0:
            stats["flips"] += 1
            new_cycle = _OpenCycle(fill.wallet, coin, end_sign, fill.time, end, max_abs_position=abs(end))
            new_cycle.add_entry(open_qty, fill.price)
            new_cycle.add_fill(fill, Decimal("1") - close_fraction)
            active[coin] = new_cycle
        elif not closes_cycle:
            cycle.position = end
            cycle.max_abs_position = max(cycle.max_abs_position, abs(end))

    stats["open_at_end"] = len(active)
    cycles.sort(key=lambda cycle: (cycle.close_time, cycle.coin, cycle.cycle_key))
    return cycles, stats
