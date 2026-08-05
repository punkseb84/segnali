from __future__ import annotations

import pandas as pd
import pytest

from project.robust_volume import add_robust_volume_features


def _frame(volumes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "close": [100.0] * len(volumes),
            "volume": volumes,
        }
    )


def test_constant_volume_is_neutral() -> None:
    result = add_robust_volume_features(_frame([100.0] * 60))
    latest = result.iloc[-1]
    assert bool(latest["volume_data_valid"])
    assert float(latest["volume_ratio"]) == pytest.approx(1.0)


def test_low_last_candle_uses_one_hour_window() -> None:
    volumes = [100.0] * 60
    volumes[-1] = 12.2
    result = add_robust_volume_features(_frame(volumes))
    latest = result.iloc[-1]
    assert float(latest["volume_ratio_15m"]) == pytest.approx(0.122)
    assert float(latest["volume_ratio"]) == pytest.approx(0.7805)


def test_zero_last_candle_does_not_force_zero_if_previous_hour_traded() -> None:
    volumes = [100.0] * 60
    volumes[-1] = 0.0
    result = add_robust_volume_features(_frame(volumes))
    latest = result.iloc[-1]
    assert float(latest["volume_ratio_15m"]) == pytest.approx(0.0)
    assert float(latest["volume_ratio"]) == pytest.approx(0.75)


def test_insufficient_history_uses_neutral_fallback() -> None:
    result = add_robust_volume_features(_frame([100.0] * 6))
    latest = result.iloc[-1]
    assert not bool(latest["volume_data_valid"])
    assert float(latest["volume_ratio"]) == pytest.approx(1.0)
