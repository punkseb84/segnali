import unittest
from datetime import datetime, timezone

try:
    import pandas as pd
    from strategy import closed_candles
except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard for minimal sandboxes
    pd = None
    closed_candles = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


class StrategyCandleHandlingTest(unittest.TestCase):
    @unittest.skipIf(IMPORT_ERROR is not None, "pandas is required for strategy candle tests")
    def test_closed_candles_removes_current_open_candle(self):
        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    [
                        "2026-07-07T20:10:00Z",
                        "2026-07-07T20:15:00Z",
                        "2026-07-07T20:20:00Z",
                    ],
                    utc=True,
                ),
                "close": [1.0, 2.0, 3.0],
            }
        )
        now = datetime(2026, 7, 7, 20, 23, tzinfo=timezone.utc)
        result = closed_candles(frame, "5m", now=now)
        self.assertEqual(result["close"].tolist(), [1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
