"""LONG-only signal generation for conservative and fast scalping modes."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from config import ACTIVE_MODE, DIRECTION, ENFORCE_SETUP_RULES, EXCHANGE_NAME, MIN_NET_RR, TRADE_AMOUNT_EUR
from indicators import classify_market_regime
from risk import TradePlan, best_trade_plan
from scoring import ScoreResult, score_long

logger = logging.getLogger("crypto-bot.strategy")


def signal_id(pair: str, when: datetime, mode: str) -> str:
    clean_pair = "".join(ch for ch in pair.upper() if ch.isalnum())
    return f"SIG-{when:%Y%m%d-%H%M}-{clean_pair}-{mode}-LONG"


def distance_pct(value: float, reference: float) -> float:
    return (value - reference) / reference * 100 if reference else 0.0


def _btc_trend(btc_data: pd.DataFrame | None) -> str:
    if btc_data is None or btc_data.empty:
        return "UNKNOWN"
    latest = btc_data.iloc[-1]
    if latest["close"] >= latest["ema200"]:
        return "BULLISH_OR_NEUTRAL"
    if latest["adx14"] >= 25:
        return "STRONG_BEARISH"
    return "WEAK_BEARISH"


def build_signal(pair: str, main: pd.DataFrame, confirm_1: pd.DataFrame, confirm_2: pd.DataFrame, btc: pd.DataFrame | None, mode: str = ACTIVE_MODE) -> dict[str, Any] | None:
    """Build an OPERATIVE/WATCHLIST/REJECTED signal record, or None for unusable data."""
    if min(len(main), len(confirm_1), len(confirm_2)) < 220:
        return None
    latest = main.iloc[-1]
    previous = main.iloc[-2]
    required = ["ema20", "ema50", "ema200", "rsi14", "macd_hist", "atr14", "adx14", "avg_volume_20", "support", "resistance", "swing_high", "swing_low"]
    if any(pd.isna(latest.get(column)) for column in required):
        return None

    regime = classify_market_regime(main)
    score = score_long(main, confirm_1, confirm_2, btc, regime, mode)
    entry = float(latest["close"])
    plan = best_trade_plan(entry, float(latest["atr14"]), float(latest["support"]), float(latest["resistance"]), float(latest["swing_high"]), float(latest["swing_low"]), regime)
    if plan is None:
        score.penalties.append(f"nessuna combinazione TP/SL con RR netto >= {MIN_NET_RR}")
        if score.decision == "OPERATIVE":
            score.decision = "WATCHLIST"

    when = datetime.now(timezone.utc)
    plan_values = plan or TradePlan(entry, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    if score.decision == "OPERATIVE" and plan is None:
        score.decision = "REJECTED"

    record: dict[str, Any] = {
        "signal_id": signal_id(pair, when, mode),
        "timestamp": when.isoformat(),
        "mode": mode,
        "exchange": EXCHANGE_NAME,
        "pair": pair,
        "direction": DIRECTION,
        "timeframe": "5m" if mode == "SCALPING_FAST" else "15m",
        "entry": entry,
        "stop_loss": plan_values.stop_loss,
        "target_1": plan_values.target_1,
        "trade_amount_eur": TRADE_AMOUNT_EUR,
        "quantity": plan_values.quantity,
        "net_profit_tp1_eur": plan_values.net_profit_tp1,
        "net_loss_sl_eur": plan_values.net_loss_sl,
        "realized_result_eur": 0.0,
        "fees_total_eur": plan_values.buy_fee + plan_values.sell_fee_tp,
        "net_rr": plan_values.net_rr,
        "score": score.score,
        "status": score.decision if score.decision != "OPERATIVE" else "OPEN",
        "outcome": None,
        "market_regime": regime,
        "btc_trend": _btc_trend(btc),
        "ema20": float(latest["ema20"]),
        "ema50": float(latest["ema50"]),
        "ema200": float(latest["ema200"]),
        "rsi": float(latest["rsi14"]),
        "macd_hist": float(latest["macd_hist"]),
        "atr": float(latest["atr14"]),
        "adx": float(latest["adx14"]),
        "volume": float(latest["volume"]),
        "avg_volume_20": float(latest["avg_volume_20"]),
        "relative_volume": float(latest["relative_volume"]),
        "support": float(latest["support"]),
        "resistance": float(latest["resistance"]),
        "swing_high": float(latest["swing_high"]),
        "swing_low": float(latest["swing_low"]),
        "distance_ema20_pct": abs(distance_pct(entry, float(latest["ema20"]))),
        "distance_resistance_pct": distance_pct(float(latest["resistance"]), entry),
        "hour": when.hour,
        "weekday": when.strftime("%A"),
        "reasons": score.reasons,
        "penalties": score.penalties,
        "ambiguous_candle": 0,
        "created_at": when.isoformat(),
        "closed_at": None,
    }

    if mode == "CONSERVATIVE":
        hard_penalties: list[str] = []
        if confirm_1.iloc[-1]["close"] <= confirm_1.iloc[-1]["ema200"]:
            hard_penalties.append("trend 1h non sopra EMA200")
        if confirm_2.iloc[-1]["close"] <= confirm_2.iloc[-1]["ema200"]:
            hard_penalties.append("trend 4h non sopra EMA200")
        if latest["close"] <= latest["ema200"]:
            hard_penalties.append("prezzo non sopra EMA200")
        if not (latest["ema20"] > latest["ema50"] > latest["ema200"]):
            hard_penalties.append("EMA20 > EMA50 > EMA200 non confermato")
        if not (50 <= latest["rsi14"] <= 65):
            hard_penalties.append("RSI fuori range 50-65")
        if latest["macd_hist"] <= 0:
            hard_penalties.append("MACD histogram non positivo")
        if latest["relative_volume"] < 1.0:
            hard_penalties.append("relative volume sotto 1.0")
        if record["btc_trend"] == "STRONG_BEARISH":
            hard_penalties.append("BTC in forte trend ribassista")
        if record["distance_resistance_pct"] <= 0.15:
            hard_penalties.append("spazio verso resistenza insufficiente")
        if hard_penalties:
            record["penalties"].extend(hard_penalties)
            if ENFORCE_SETUP_RULES and record["status"] == "OPEN":
                record["status"] = "WATCHLIST"
    else:
        hard_penalties = []
        if not (confirm_1.iloc[-1]["close"] > confirm_1.iloc[-1]["ema200"] or latest["close"] > latest["ema200"]):
            hard_penalties.append("trend 15m non favorevole e prezzo 5m non sopra EMA200")
        if latest["ema20"] <= latest["ema50"]:
            hard_penalties.append("EMA20 <= EMA50 sul 5m")
        if not (48 <= latest["rsi14"] <= 68):
            hard_penalties.append("RSI 5m fuori range 48-68")
        if not (latest["macd_hist"] > 0 or latest["macd_hist"] > previous["macd_hist"]):
            hard_penalties.append("MACD histogram non positivo né crescente")
        if latest["relative_volume"] < 0.8:
            hard_penalties.append("volume sotto 0.8 della media 20")
        if record["distance_resistance_pct"] <= 0.15:
            hard_penalties.append("spazio verso resistenza insufficiente per TP1")
        if record["btc_trend"] == "STRONG_BEARISH":
            hard_penalties.append("BTC in forte trend ribassista")
        if hard_penalties:
            record["penalties"].extend(hard_penalties)
            if ENFORCE_SETUP_RULES and record["status"] == "OPEN":
                record["status"] = "WATCHLIST"

    if plan is None:
        record["status"] = "REJECTED"
    return record
