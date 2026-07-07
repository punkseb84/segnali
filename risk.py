"""Risk, target/stop selection and euro PnL calculations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from config import FEE_BUY_PERCENT, FEE_SELL_PERCENT, MIN_NET_RR, SLIPPAGE_PERCENT, SPREAD_PERCENT, TRADE_AMOUNT_EUR


@dataclass(frozen=True)
class TradePlan:
    entry: float
    stop_loss: float
    target_1: float
    quantity: float
    gross_profit: float
    gross_loss: float
    buy_fee: float
    sell_fee_tp: float
    sell_fee_sl: float
    spread_cost: float
    slippage_cost: float
    net_profit_tp1: float
    net_loss_sl: float
    net_rr: float


def format_price(value: float, min_decimals: int = 4) -> str:
    decimals = max(min_decimals, 6 if abs(value) < 1 else 4)
    return f"{value:.{decimals}f}"


def calculate_plan(entry: float, stop_loss: float, target_1: float) -> TradePlan | None:
    """Calculate all euro metrics for one LONG plan."""
    if entry <= 0 or stop_loss <= 0 or target_1 <= 0 or not (stop_loss < entry < target_1):
        return None
    quantity = TRADE_AMOUNT_EUR / entry
    gross_profit = quantity * (target_1 - entry)
    gross_loss = quantity * (entry - stop_loss)
    buy_fee = TRADE_AMOUNT_EUR * FEE_BUY_PERCENT / 100
    sell_fee_tp = quantity * target_1 * FEE_SELL_PERCENT / 100
    sell_fee_sl = quantity * stop_loss * FEE_SELL_PERCENT / 100
    spread_cost = TRADE_AMOUNT_EUR * SPREAD_PERCENT / 100
    slippage_cost = TRADE_AMOUNT_EUR * SLIPPAGE_PERCENT / 100
    net_profit_tp1 = gross_profit - buy_fee - sell_fee_tp - spread_cost - slippage_cost
    net_loss_sl = gross_loss + buy_fee + sell_fee_sl + spread_cost + slippage_cost
    if net_loss_sl <= 0:
        return None
    return TradePlan(
        entry=entry,
        stop_loss=stop_loss,
        target_1=target_1,
        quantity=quantity,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        buy_fee=buy_fee,
        sell_fee_tp=sell_fee_tp,
        sell_fee_sl=sell_fee_sl,
        spread_cost=spread_cost,
        slippage_cost=slippage_cost,
        net_profit_tp1=net_profit_tp1,
        net_loss_sl=net_loss_sl,
        net_rr=net_profit_tp1 / net_loss_sl,
    )


def _unique_positive(values: Iterable[float]) -> list[float]:
    return sorted({round(value, 10) for value in values if value > 0})


def best_trade_plan(entry: float, atr: float, support: float, resistance: float, swing_high: float, swing_low: float, regime: str) -> TradePlan | None:
    """Generate multiple dynamic TP/SL combinations and pick the best valid one."""
    if atr <= 0:
        return None
    buffer = max(atr * 0.08, entry * 0.0005)
    targets = _unique_positive([resistance - buffer, swing_high - buffer, entry + 0.6 * atr, entry + 0.8 * atr, entry + 1.0 * atr])
    stops = _unique_positive([swing_low - buffer, support - buffer, entry - 0.6 * atr, entry - 0.8 * atr, entry - 1.0 * atr])
    plans: list[tuple[float, TradePlan]] = []
    for target in targets:
        for stop in stops:
            plan = calculate_plan(entry, stop, target)
            if plan is None or plan.net_rr < MIN_NET_RR:
                continue
            resistance_score = max(0.0, resistance - target) / max(entry, 1e-9)
            volatility_score = 0.05 if regime == "TREND_STRONG" else 0.0
            score = plan.net_rr + resistance_score + volatility_score
            plans.append((score, plan))
    if not plans:
        return None
    return max(plans, key=lambda item: item[0])[1]
