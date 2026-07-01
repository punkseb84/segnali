import unittest

from trade_utils import evaluate_trade_candles, format_price


def make_trade(**overrides):
    trade = {
        "symbol": "TEST/USD",
        "direction": "LONG",
        "entry": 74.100,
        "stop_loss": 73.440,
        "target_1": 75.090,
        "target_2": 76.080,
        "status": "open",
        "target_1_hit": False,
        "target_2_hit": False,
        "stop_loss_hit": False,
        "opened_at": "2026-07-01T03:46:00+00:00",
        "candle_timestamp": "1782876600000",
        "result": "OPEN",
        "result_r": None,
        "analyzed_candles": 0,
        "min_after_entry": None,
        "max_after_entry": None,
        "decisive_candle_timestamp": None,
        "decisive_candle_ohlc": None,
        "last_checked_candle_timestamp": None,
        "check_from_candle_timestamp": None,
    }
    trade.update(overrides)
    return trade


class TradeEvaluationTest(unittest.TestCase):
    def test_false_stop_is_not_assigned_when_low_never_reaches_stop(self):
        trade = make_trade()
        candles = [
            {"timestamp": 1782878400000, "open": 74.2, "high": 74.8, "low": 73.441, "close": 74.5},
            {"timestamp": 1782879300000, "open": 74.5, "high": 74.9, "low": 73.500, "close": 74.7},
        ]

        event = evaluate_trade_candles(trade, candles, "15m")

        self.assertIsNone(event)
        self.assertEqual(trade["status"], "open")
        self.assertFalse(trade["stop_loss_hit"])

    def test_long_target_1_is_detected_from_high(self):
        trade = make_trade()
        candles = [
            {"timestamp": 1782878400000, "open": 74.2, "high": 75.100, "low": 73.900, "close": 74.9},
        ]

        event = evaluate_trade_candles(trade, candles, "15m")

        self.assertEqual(event, "TARGET_1")
        self.assertTrue(trade["target_1_hit"])
        self.assertEqual(trade["result"], "TP1")

    def test_ambiguous_candle_is_not_forced_to_stop_or_target(self):
        trade = make_trade()
        candles = [
            {"timestamp": 1782878400000, "open": 74.2, "high": 75.100, "low": 73.430, "close": 74.8},
        ]

        event = evaluate_trade_candles(trade, candles, "15m")

        self.assertEqual(event, "AMBIGUOUS")
        self.assertEqual(trade["status"], "ambiguous")
        self.assertEqual(trade["result"], "AMBIGUO")
        self.assertFalse(trade["stop_loss_hit"])
        self.assertFalse(trade["target_1_hit"])

    def test_price_format_keeps_at_least_three_decimals(self):
        self.assertEqual(format_price(1.204), "1.204")
        self.assertEqual(format_price(1.198), "1.198")
        self.assertNotEqual(format_price(1.204), "1.20")
        self.assertNotEqual(format_price(1.198), "1.20")

    def test_candle_opened_before_signal_is_ignored(self):
        trade = make_trade()
        candles = [
            {"timestamp": 1782877500000, "open": 74.2, "high": 74.8, "low": 73.000, "close": 74.5},
            {"timestamp": 1782878400000, "open": 74.5, "high": 74.9, "low": 73.900, "close": 74.7},
        ]

        event = evaluate_trade_candles(trade, candles, "15m")

        self.assertIsNone(event)
        self.assertEqual(trade["status"], "open")
        self.assertEqual(trade["analyzed_candles"], 1)


if __name__ == "__main__":
    unittest.main()
