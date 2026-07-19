"""Confirmed-pivot detection for bullish Gartley, Bat, Butterfly and Crab patterns."""
from __future__ import annotations

from typing import Any

import pandas as pd


HARMONIC_STRATEGY = "Harmonic Pattern Reversal"


def confirmed_pivots(
    frame: pd.DataFrame,
    lookback: int = 140,
    left: int = 3,
    right: int = 2,
) -> list[dict[str, Any]]:
    data = frame.tail(lookback).reset_index(drop=True)
    pivots: list[dict[str, Any]] = []
    if len(data) < left + right + 10:
        return pivots
    for index in range(left, len(data) - right):
        window = data.iloc[index - left : index + right + 1]
        high = float(data.iloc[index]["high"])
        low = float(data.iloc[index]["low"])
        is_high = high >= float(window["high"].max())
        is_low = low <= float(window["low"].min())
        if is_high == is_low:
            continue
        pivot = {
            "kind": "H" if is_high else "L",
            "index": index,
            "price": high if is_high else low,
            "timestamp": data.iloc[index].get("timestamp"),
        }
        if pivots and pivots[-1]["kind"] == pivot["kind"]:
            replace_previous = (
                pivot["price"] > pivots[-1]["price"]
                if pivot["kind"] == "H"
                else pivot["price"] < pivots[-1]["price"]
            )
            if replace_previous:
                pivots[-1] = pivot
        else:
            pivots.append(pivot)
    return pivots


def classify_ratios(
    ab_xa: float,
    bc_ab: float,
    cd_bc: float,
    ad_xa: float,
) -> tuple[str | None, float]:
    specs = {
        "Gartley": ((0.55, 0.70, 0.618), (0.382, 0.886, 0.618), (1.13, 1.75, 1.414), (0.72, 0.86, 0.786)),
        "Bat": ((0.32, 0.55, 0.45), (0.382, 0.886, 0.618), (1.55, 2.75, 2.00), (0.82, 0.95, 0.886)),
        "Butterfly": ((0.72, 0.86, 0.786), (0.382, 0.886, 0.618), (1.55, 2.40, 1.90), (1.18, 1.40, 1.272)),
        "Crab": ((0.32, 0.68, 0.50), (0.382, 0.886, 0.618), (2.00, 3.90, 3.00), (1.45, 1.80, 1.618)),
    }
    values = (ab_xa, bc_ab, cd_bc, ad_xa)
    best_name: str | None = None
    best_quality = 0.0
    for name, ranges in specs.items():
        if not all(low <= value <= high for value, (low, high, _) in zip(values, ranges)):
            continue
        error = sum(
            abs(value - ideal) / max(high - low, 1e-9)
            for value, (low, high, ideal) in zip(values, ranges)
        )
        quality = max(0.0, 1.0 - error / 4.0)
        if quality > best_quality:
            best_name, best_quality = name, quality
    return best_name, best_quality


def detect_bullish_harmonic(frame: pd.DataFrame) -> dict[str, Any] | None:
    pivots = confirmed_pivots(frame)
    if len(pivots) < 5:
        return None
    data = frame.tail(140).reset_index(drop=True)
    atr = float(data.iloc[-1].get("atr", 0.0) or 0.0)
    best: dict[str, Any] | None = None
    for start in range(max(0, len(pivots) - 16), len(pivots) - 4):
        points = pivots[start : start + 5]
        if [item["kind"] for item in points] != ["L", "H", "L", "H", "L"]:
            continue
        x, a, b, c, d = [float(item["price"]) for item in points]
        xa, ab, bc, cd, ad = a - x, a - b, c - b, c - d, a - d
        if min(xa, ab, bc, cd, ad) <= 0 or (atr > 0 and xa < atr * 3.0):
            continue
        pattern, quality = classify_ratios(ab / xa, bc / ab, cd / bc, ad / xa)
        if pattern is None:
            continue
        bars_after_d = len(data) - 1 - int(points[4]["index"])
        if not 2 <= bars_after_d <= 7:
            continue
        candidate = {
            "pattern": pattern,
            "quality": quality,
            "bars_after_d": bars_after_d,
            "points": {"X": x, "A": a, "B": b, "C": c, "D": d},
            "target_382": d + (a - d) * 0.382,
            "pivot_time": points[4].get("timestamp"),
        }
        if best is None or quality > float(best["quality"]):
            best = candidate
    return best
