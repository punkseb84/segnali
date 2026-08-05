"""Robust relative-volume features for closed crypto candles.

The old implementation compared one 15m candle with a rolling mean that included
that same candle. This made the filter extremely noisy and allowed one low/zero
candle to veto an otherwise valid setup. The robust feature uses the sum of the
last four closed 15m candles (one hour) against the median of previous one-hour
windows. Baselines are shifted, so the current window never contaminates its own
reference value.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def add_robust_volume_features(
    frame: pd.DataFrame,
    *,
    aggregate_window: int = 4,
    baseline_windows: int = 24,
    min_history_windows: int = 8,
) -> pd.DataFrame:
    """Return ``frame`` with robust 15m and one-hour relative-volume features.

    ``volume_ratio`` is the one-hour ratio used by the strategies. When history is
    insufficient or the baseline is invalid, volume is treated as neutral (1.0)
    and ``volume_data_valid`` is false, rather than silently becoming zero.
    """
    data = frame.copy()
    if "volume" not in data.columns:
        raise ValueError("volume column is required")

    volume = pd.to_numeric(data["volume"], errors="coerce")
    volume = volume.where(volume >= 0)
    data["volume"] = volume

    previous_15m_median = volume.shift(1).rolling(20, min_periods=8).median()
    ratio_15m = volume / previous_15m_median.replace(0, np.nan)

    one_hour_volume = volume.rolling(
        aggregate_window,
        min_periods=aggregate_window,
    ).sum()
    previous_one_hour_median = one_hour_volume.shift(1).rolling(
        baseline_windows,
        min_periods=min_history_windows,
    ).median()
    ratio_1h = one_hour_volume / previous_one_hour_median.replace(0, np.nan)

    nonzero_history = (volume.shift(1) > 0).rolling(20, min_periods=8).sum()
    data_valid = previous_one_hour_median.gt(0) & nonzero_history.ge(8)

    clean_ratio_15m = ratio_15m.replace([np.inf, -np.inf], np.nan).fillna(1.0).clip(0.0, 5.0)
    clean_ratio_1h = ratio_1h.replace([np.inf, -np.inf], np.nan).fillna(1.0).clip(0.0, 5.0)
    robust_ratio = clean_ratio_1h.where(data_valid, 1.0)

    # Keep legacy field names for downstream compatibility.
    data["volume_avg20"] = previous_15m_median
    data["volume_ratio_15m"] = clean_ratio_15m
    data["volume_window_1h"] = one_hour_volume
    data["volume_baseline_1h"] = previous_one_hour_median
    data["volume_ratio_1h"] = clean_ratio_1h
    data["volume_data_valid"] = data_valid.fillna(False)
    data["volume_ratio"] = robust_ratio.fillna(1.0).clip(0.0, 5.0)
    return data
