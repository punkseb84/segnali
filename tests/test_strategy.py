import unittest
from datetime import datetime, timezone

import pandas as pd

from strategy import closed_candles


class StrategyCandleHandlingTest(unittest.TestCase):
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
