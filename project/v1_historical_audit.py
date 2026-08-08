"""Historical audit for the original 2026-06-25 Kraken signal bot.

Diagnostic only. Reconstructs original V1 signals from stored OHLCV and evaluates
whether TP1/SL would have been reached first. Does not alter live Velez trading.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import math

import pandas as pd


SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "BNB/USD", "DOGE/USD"]


@dataclass
class Trade:
    pair: str
    direction: str
    time: Any
    entry: float
    stop: float
    tp1: float
    tp2: float
    atr: float
    outcome: str = "OPEN"
    exit_price: float | None = None
    exit_time: Any | None = None
    mfe_r: float = 0.0
    mae_r: float = 0.0
    tp2_touched: bool = False


class V1HistoricalAudit:
    def __init__(self, postgres: Any, logger: Any,
                 notional_eur: float = 25.0,
                 buy_fee_rate: float = 0.001,
                 sell_fee_rate: float = 0.001,
                 spread_rate: float = 0.0005,
                 slippage_rate: float = 0.0) -> None:
        self.postgres = postgres
        self.logger = logger
        self.notional_eur = float(notional_eur)
        self.buy_fee_rate = float(buy_fee_rate)
        self.sell_fee_rate = float(sell_fee_rate)
        self.spread_rate = float(spread_rate)
        self.slippage_rate = float(slippage_rate)

    def _frame(self, pair: str, timeframe: str) -> pd.DataFrame:
        rows = self.postgres.fetch_all(
            """SELECT timestamp,open,high,low,close,volume
                 FROM market_data.ohlc
                WHERE LOWER(TRIM(exchange))='kraken'
                  AND pair=%s AND timeframe=%s
                ORDER BY timestamp ASC""",
            (pair, timeframe),
        )
        frame = pd.DataFrame(rows, columns=["timestamp","open","high","low","close","volume"])
        if frame.empty:
            return frame
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        for c in ["open","high","low","close","volume"]:
            frame[c] = pd.to_numeric(frame[c], errors="coerce")
        return frame.dropna().reset_index(drop=True)

    @staticmethod
    def _indicators(frame: pd.DataFrame) -> pd.DataFrame:
        d = frame.copy()
        d["ema20"] = d["close"].ewm(span=20, adjust=False).mean()
        d["ema50"] = d["close"].ewm(span=50, adjust=False).mean()
        d["ema200"] = d["close"].ewm(span=200, adjust=False).mean()
        delta = d["close"].diff()
        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)
        avg_gain = gain.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        avg_loss = loss.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        rs = avg_gain / avg_loss.replace(0, math.nan)
        d["rsi14"] = 100 - (100 / (1 + rs))
        ema12 = d["close"].ewm(span=12, adjust=False).mean()
        ema26 = d["close"].ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        d["macd_hist"] = macd - signal
        prev_close = d["close"].shift(1)
        tr = pd.concat([
            (d["high"]-d["low"]).abs(),
            (d["high"]-prev_close).abs(),
            (d["low"]-prev_close).abs(),
        ], axis=1).max(axis=1)
        d["atr14"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        d["volume_avg20"] = d["volume"].rolling(20).mean()
        return d

    @staticmethod
    def _align_trend(main_ts: pd.Timestamp, trend: pd.DataFrame) -> pd.Series | None:
        eligible = trend[trend["timestamp"] <= main_ts.floor("1h")]
        if eligible.empty:
            return None
        return eligible.iloc[-1]

    def _signals_for_pair(self, pair: str) -> tuple[list[Trade], dict[str, Any]]:
        main = self._frame(pair, "15m")
        trend = self._frame(pair, "1h")
        coverage = {
            "pair": pair,
            "bars15": len(main), "bars1h": len(trend),
            "start15": str(main["timestamp"].iloc[0]) if not main.empty else None,
            "end15": str(main["timestamp"].iloc[-1]) if not main.empty else None,
            "has_volume": bool(not main.empty and main["volume"].notna().all()),
        }
        if len(main) < 220 or len(trend) < 205:
            return [], coverage
        main = self._indicators(main)
        trend = self._indicators(trend)
        trades: list[Trade] = []
        last_key: dict[str, pd.Timestamp] = {}
        for i in range(1, len(main)):
            prev, cur = main.iloc[i-1], main.iloc[i]
            needed = [cur.get(k) for k in ["ema20","ema50","ema200","rsi14","macd_hist","atr14","volume_avg20"]]
            if any(pd.isna(v) for v in needed) or pd.isna(prev["macd_hist"]):
                continue
            tr = self._align_trend(cur["timestamp"], trend)
            if tr is None or pd.isna(tr.get("ema200")):
                continue
            bull1h = float(tr["close"]) > float(tr["ema200"])
            bear1h = float(tr["close"]) < float(tr["ema200"])
            bull_emas = float(cur["ema20"]) > float(cur["ema50"]) > float(cur["ema200"])
            bear_emas = float(cur["ema20"]) < float(cur["ema50"]) < float(cur["ema200"])
            vol_ok = float(cur["volume"]) > float(cur["volume_avg20"])
            cross_up = float(prev["macd_hist"]) < 0 and float(cur["macd_hist"]) > 0
            cross_dn = float(prev["macd_hist"]) > 0 and float(cur["macd_hist"]) < 0
            long_ok = bull1h and bull_emas and 50 <= float(cur["rsi14"]) <= 70 and cross_up and vol_ok
            short_ok = bear1h and bear_emas and 30 <= float(cur["rsi14"]) <= 50 and cross_dn and vol_ok
            if not long_ok and not short_ok:
                continue
            direction = "LONG" if long_ok else "SHORT"
            key = f"{pair}:{direction}"
            candle_ts = pd.Timestamp(cur["timestamp"])
            if last_key.get(key) == candle_ts:
                continue
            last_key[key] = candle_ts
            entry = float(cur["close"])
            atr = float(cur["atr14"])
            risk = 1.5 * atr
            if risk <= 0:
                continue
            if direction == "LONG":
                stop, tp1, tp2 = entry-risk, entry+1.5*risk, entry+3*risk
            else:
                stop, tp1, tp2 = entry+risk, entry-1.5*risk, entry-3*risk
            trades.append(Trade(pair,direction,candle_ts,entry,stop,tp1,tp2,atr))
        return trades, coverage

    @staticmethod
    def _resolve_trade(trade: Trade, future: pd.DataFrame) -> Trade:
        risk = abs(trade.entry-trade.stop)
        max_fav = 0.0
        max_adv = 0.0
        for _, row in future.iterrows():
            high, low = float(row["high"]), float(row["low"])
            if trade.direction == "LONG":
                max_fav = max(max_fav, high-trade.entry)
                max_adv = max(max_adv, trade.entry-low)
                sl = low <= trade.stop
                tp1 = high >= trade.tp1
                tp2 = high >= trade.tp2
            else:
                max_fav = max(max_fav, trade.entry-low)
                max_adv = max(max_adv, high-trade.entry)
                sl = high >= trade.stop
                tp1 = low <= trade.tp1
                tp2 = low <= trade.tp2
            trade.tp2_touched = trade.tp2_touched or tp2
            trade.mfe_r = max_fav/risk if risk else 0.0
            trade.mae_r = max_adv/risk if risk else 0.0
            if sl and tp1:
                trade.outcome = "AMBIGUOUS"
                trade.exit_price = float(row["close"])
                trade.exit_time = row["timestamp"]
                return trade
            if sl:
                trade.outcome = "SL"
                trade.exit_price = trade.stop
                trade.exit_time = row["timestamp"]
                return trade
            if tp1:
                trade.outcome = "TP1"
                trade.exit_price = trade.tp1
                trade.exit_time = row["timestamp"]
                return trade
        trade.outcome = "UNRESOLVED"
        if not future.empty:
            trade.exit_price = float(future.iloc[-1]["close"])
            trade.exit_time = future.iloc[-1]["timestamp"]
        return trade

    def _net_eur(self, trade: Trade) -> float:
        if trade.exit_price is None:
            return 0.0
        qty = self.notional_eur / trade.entry
        gross = qty * ((trade.exit_price-trade.entry) if trade.direction=="LONG" else (trade.entry-trade.exit_price))
        entry_fee = self.notional_eur*self.buy_fee_rate
        exit_notional = qty*trade.exit_price
        exit_fee = exit_notional*self.sell_fee_rate
        spread = qty*(trade.entry+trade.exit_price)*(self.spread_rate/2)
        slippage = qty*(trade.entry+trade.exit_price)*self.slippage_rate
        return gross-entry_fee-exit_fee-spread-slippage

    def run(self) -> dict[str, Any]:
        self.logger.info("V1_HISTORICAL_AUDIT_START")
        all_trades: list[Trade] = []
        coverage = []
        frames: dict[str,pd.DataFrame] = {}
        for pair in SYMBOLS:
            trades, cov = self._signals_for_pair(pair)
            coverage.append(cov)
            frame = self._frame(pair,"15m")
            frames[pair] = frame
            for trade in trades:
                future = frame[frame["timestamp"] > trade.time]
                all_trades.append(self._resolve_trade(trade, future))
        resolved = [t for t in all_trades if t.outcome in {"TP1","SL"}]
        ambiguous = [t for t in all_trades if t.outcome == "AMBIGUOUS"]
        unresolved = [t for t in all_trades if t.outcome == "UNRESOLVED"]
        wins = [t for t in resolved if t.outcome == "TP1"]
        losses = [t for t in resolved if t.outcome == "SL"]
        net = sum(self._net_eur(t) for t in resolved)
        gross_wins = sum(max(self._net_eur(t),0) for t in resolved)
        gross_losses = sum(max(-self._net_eur(t),0) for t in resolved)
        pf = gross_wins/gross_losses if gross_losses>0 else None
        expectancy = net/len(resolved) if resolved else 0.0
        equity = 100.0
        peak = equity
        max_dd = 0.0
        for t in sorted(resolved, key=lambda x: x.exit_time):
            equity += self._net_eur(t)
            peak = max(peak,equity)
            max_dd = max(max_dd, peak-equity)
        summary = {
            "signals": len(all_trades), "resolved": len(resolved),
            "wins": len(wins), "losses": len(losses),
            "ambiguous": len(ambiguous), "unresolved": len(unresolved),
            "win_rate": len(wins)/len(resolved) if resolved else 0.0,
            "net_eur": net, "ending_capital": 100.0+net,
            "profit_factor": pf, "expectancy_eur": expectancy,
            "max_drawdown_eur": max_dd,
            "tp2_touched": sum(1 for t in all_trades if t.tp2_touched),
            "avg_mfe_r": sum(t.mfe_r for t in all_trades)/len(all_trades) if all_trades else 0.0,
            "avg_mae_r": sum(t.mae_r for t in all_trades)/len(all_trades) if all_trades else 0.0,
            "coverage": coverage,
        }
        self.logger.warning("V1_HISTORICAL_AUDIT_SUMMARY %s", summary)
        return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        pf = s.get("profit_factor")
        pf_text = "n/d" if pf is None else f"{pf:.2f}"
        cov_lines = []
        for c in s.get("coverage",[]):
            cov_lines.append(f"• {c['pair']}: 15m={c['bars15']} · 1h={c['bars1h']} · volume={'OK' if c['has_volume'] else 'NO'}")
        return (
            "📊 <b>AUDIT STORICO BOT V1 ORIGINALE</b>\n"
            f"Segnali ricostruiti: <b>{s.get('signals',0)}</b>\n"
            f"Trade risolti: <b>{s.get('resolved',0)}</b>\n"
            f"🟢 TP1: <b>{s.get('wins',0)}</b> · 🔴 SL: <b>{s.get('losses',0)}</b>\n"
            f"⚪ Ambigui: <b>{s.get('ambiguous',0)}</b> · irrisolti: <b>{s.get('unresolved',0)}</b>\n"
            f"Win rate: <b>{100*s.get('win_rate',0):.1f}%</b>\n"
            f"TP2 toccato prima/dopo TP1: <b>{s.get('tp2_touched',0)}</b>\n"
            f"P&L netto stimato: <b>€{s.get('net_eur',0):+.2f}</b>\n"
            f"Capitale teorico: <b>€100,00 → €{s.get('ending_capital',100):.2f}</b>\n"
            f"Profit Factor: <b>{pf_text}</b>\n"
            f"Expectancy: <b>€{s.get('expectancy_eur',0):+.3f}/trade</b>\n"
            f"Max drawdown: <b>€{s.get('max_drawdown_eur',0):.2f}</b>\n"
            f"MFE medio: <b>+{s.get('avg_mfe_r',0):.2f}R</b> · MAE medio: <b>-{s.get('avg_mae_r',0):.2f}R</b>\n"
            "\n<b>Copertura dati</b>\n" + "\n".join(cov_lines) +
            "\n\n⚠️ Backtest diagnostico: regole V1 originali, TP1 come uscita principale; stessa candela SL+TP1 = ambiguo."
        )
