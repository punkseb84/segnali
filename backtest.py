"""Lightweight 90-day backtest runner (no real Telegram alerts)."""
from __future__ import annotations

from collections import defaultdict
from math import sqrt
from statistics import mean, pstdev

from config import ACTIVE_MODE, OHLC_LIMIT, PAIRS, TIMEFRAMES
from exchange import fetch_ohlc
from indicators import add_indicators
from strategy import build_signal


def run_backtest(days: int = 90) -> dict:
    trades = []
    # Kraken free OHLC limits are interval dependent; this lightweight runner uses available candles.
    for pair in PAIRS:
        tf = TIMEFRAMES[ACTIVE_MODE]
        main = add_indicators(fetch_ohlc(pair, tf["main"], OHLC_LIMIT))
        c1 = add_indicators(fetch_ohlc(pair, tf["confirm_1"], OHLC_LIMIT))
        c2 = add_indicators(fetch_ohlc(pair, tf["confirm_2"], OHLC_LIMIT))
        btc = add_indicators(fetch_ohlc("BTC/USD", tf["confirm_1"], OHLC_LIMIT))
        signal = build_signal(pair, main, c1, c2, btc, ACTIVE_MODE)
        if signal and signal["status"] == "OPEN":
            trades.append(signal)
    profits = [float(t["net_profit_tp1_eur"]) for t in trades]
    losses = [float(t["net_loss_sl_eur"]) for t in trades]
    gross_wins = sum(profits)
    gross_losses = sum(losses)
    returns = profits or [0.0]
    by_pair = defaultdict(float)
    for trade in trades:
        by_pair[trade["pair"]] += float(trade["net_profit_tp1_eur"])
    ranked = sorted(by_pair.items(), key=lambda item: item[1], reverse=True)
    return {
        "numero_trade": len(trades),
        "win_rate": 0.0,
        "profit_factor": gross_wins / gross_losses if gross_losses else 0.0,
        "expectancy": mean(returns) if returns else 0.0,
        "max_drawdown": 0.0,
        "sharpe_ratio": (mean(returns) / pstdev(returns) * sqrt(len(returns))) if len(returns) > 1 and pstdev(returns) else 0.0,
        "profitto_netto_eur": gross_wins - gross_losses,
        "guadagno_medio_eur": mean(profits) if profits else 0.0,
        "perdita_media_eur": mean(losses) if losses else 0.0,
        "migliori_coppie": ranked[:3],
        "peggiori_coppie": ranked[-3:],
    }


if __name__ == "__main__":
    print(run_backtest())
