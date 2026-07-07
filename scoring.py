"""Signal scoring and decision helpers."""
from __future__ import annotations

from dataclasses import dataclass

from config import (
    ACTIVE_MODE,
    MIN_SIGNAL_SCORE_CONSERVATIVE,
    MIN_SIGNAL_SCORE_SCALPING,
    MIN_WATCHLIST_SCORE_CONSERVATIVE,
    MIN_WATCHLIST_SCORE_SCALPING,
)


@dataclass
class ScoreResult:
    score: int
    decision: str
    reasons: list[str]
    penalties: list[str]


def thresholds(mode: str = ACTIVE_MODE) -> tuple[int, int]:
    if mode == "CONSERVATIVE":
        return MIN_SIGNAL_SCORE_CONSERVATIVE, MIN_WATCHLIST_SCORE_CONSERVATIVE
    return MIN_SIGNAL_SCORE_SCALPING, MIN_WATCHLIST_SCORE_SCALPING


def score_long(main, confirm_1, confirm_2, btc, regime: str, mode: str = ACTIVE_MODE) -> ScoreResult:
    """Score a potential LONG setup from 0 to 100."""
    latest = main.iloc[-1]
    previous = main.iloc[-2]
    c1 = confirm_1.iloc[-1]
    c2 = confirm_2.iloc[-1]
    btc_latest = btc.iloc[-1] if btc is not None and not btc.empty else None
    score = 0
    reasons: list[str] = []
    penalties: list[str] = []

    trend_1_ok = c1["close"] > c1["ema200"] or c1["ema20"] > c1["ema50"]
    trend_2_ok = c2["close"] > c2["ema200"] or c2["ema20"] > c2["ema50"]
    if trend_1_ok:
        score += 20
        reasons.append("trend conferma 1 favorevole")
    if trend_2_ok:
        score += 20
        reasons.append("trend conferma 2 favorevole")
    if latest["close"] > latest["ema200"]:
        score += 10
        reasons.append("prezzo sopra EMA200")
    if latest["ema20"] > latest["ema50"] > latest["ema200"]:
        score += 10
        reasons.append("EMA20 > EMA50 > EMA200")
    elif mode == "SCALPING_FAST" and latest["ema20"] > latest["ema50"]:
        score += 10
        reasons.append("EMA20 > EMA50")
    if (mode == "SCALPING_FAST" and 48 <= latest["rsi14"] <= 68) or (mode == "CONSERVATIVE" and 50 <= latest["rsi14"] <= 65):
        score += 10
        reasons.append("RSI valido")
    if latest["macd_hist"] > 0 or (mode == "SCALPING_FAST" and latest["macd_hist"] > previous["macd_hist"]):
        score += 10
        reasons.append("MACD positivo o crescente")
    min_volume = 0.8 if mode == "SCALPING_FAST" else 1.0
    if latest["relative_volume"] >= min_volume:
        score += 10
        reasons.append("volume sufficiente")
    target_room = (latest["resistance"] - latest["close"]) / latest["close"] * 100 if latest["close"] else 0
    if target_room > 0.15:
        score += 10
        reasons.append("spazio verso resistenza")
    if btc_latest is None or btc_latest["close"] >= btc_latest["ema200"] or btc_latest["adx14"] < 25:
        score += 10
        reasons.append("BTC non ribassista")

    if latest["rsi14"] > 70:
        score -= 15
        penalties.append("RSI > 70")
    distance_ema20 = abs(latest["close"] - latest["ema20"]) / latest["close"] * 100 if latest["close"] else 0
    if distance_ema20 > 2:
        score -= 10
        penalties.append("prezzo troppo distante da EMA20")
    if target_room < 0.15:
        score -= 20
        penalties.append("resistenza troppo vicina")
    atr_pct = latest["atr14"] / latest["close"] * 100 if latest["close"] else 0
    if atr_pct > 5:
        score -= 10
        penalties.append("ATR troppo alto")
    if regime == "NO_TRADE":
        score -= 50
        penalties.append("regime NO_TRADE")

    score = max(0, min(100, int(score)))
    operative, watchlist = thresholds(mode)
    if score >= operative:
        decision = "OPERATIVE"
    elif score >= watchlist:
        decision = "WATCHLIST"
    else:
        decision = "REJECTED"
    return ScoreResult(score=score, decision=decision, reasons=reasons, penalties=penalties)
