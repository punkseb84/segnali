"""Historical audit for the original 2026-06-25 Kraken signal bot.

Diagnostic only. Reconstructs original V1 signals from stored OHLCV and evaluates
alternative exits without changing the live Velez runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
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
    intrabar_ambiguous: bool = False

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def risk_pct(self) -> float:
        return self.risk / self.entry if self.entry else 0.0


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
        return None if eligible.empty else eligible.iloc[-1]

    def _signals_for_pair(self, pair: str) -> tuple[list[Trade], dict[str, Any]]:
        main = self._frame(pair, "15m")
        trend = self._frame(pair, "1h")
        coverage = {
            "pair": pair, "bars15": len(main), "bars1h": len(trend),
            "start15": str(main["timestamp"].iloc[0]) if not main.empty else None,
            "end15": str(main["timestamp"].iloc[-1]) if not main.empty else None,
            "has_volume": bool(not main.empty and main["volume"].notna().all()),
        }
        if len(main) < 220 or len(trend) < 205:
            return [], coverage
        main = self._indicators(main)
        trend = self._indicators(trend)
        trades: list[Trade] = []
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
            entry = float(cur["close"])
            atr = float(cur["atr14"])
            risk = 1.5 * atr
            if risk <= 0:
                continue
            if direction == "LONG":
                stop, tp1, tp2 = entry-risk, entry+1.5*risk, entry+3*risk
            else:
                stop, tp1, tp2 = entry+risk, entry-1.5*risk, entry-3*risk
            trades.append(Trade(pair,direction,pd.Timestamp(cur["timestamp"]),entry,stop,tp1,tp2,atr))
        return trades, coverage

    @staticmethod
    def _resolve_fixed(trade: Trade, future: pd.DataFrame, target_r: float, *, stop_first: bool = False) -> Trade:
        t = replace(
            trade, outcome="OPEN", exit_price=None, exit_time=None,
            mfe_r=0.0, mae_r=0.0, tp2_touched=False, intrabar_ambiguous=False,
        )
        risk = t.risk
        target = t.entry + target_r*risk if t.direction == "LONG" else t.entry-target_r*risk
        max_fav = max_adv = 0.0
        for _, row in future.iterrows():
            high, low = float(row["high"]), float(row["low"])
            if t.direction == "LONG":
                max_fav, max_adv = max(max_fav, high-t.entry), max(max_adv, t.entry-low)
                sl, tp = low <= t.stop, high >= target
                t.tp2_touched = t.tp2_touched or high >= t.tp2
            else:
                max_fav, max_adv = max(max_fav, t.entry-low), max(max_adv, high-t.entry)
                sl, tp = high >= t.stop, low <= target
                t.tp2_touched = t.tp2_touched or low <= t.tp2
            t.mfe_r, t.mae_r = max_fav/risk, max_adv/risk
            if sl and tp:
                t.intrabar_ambiguous = True
                if stop_first:
                    t.outcome, t.exit_price, t.exit_time = "SL", t.stop, row["timestamp"]
                else:
                    t.outcome, t.exit_price, t.exit_time = "AMBIGUOUS", float(row["close"]), row["timestamp"]
                return t
            if sl:
                t.outcome, t.exit_price, t.exit_time = "SL", t.stop, row["timestamp"]
                return t
            if tp:
                t.outcome, t.exit_price, t.exit_time = "TP", target, row["timestamp"]
                return t
        t.outcome = "UNRESOLVED"
        if not future.empty:
            t.exit_price, t.exit_time = float(future.iloc[-1]["close"]), future.iloc[-1]["timestamp"]
        return t

    @staticmethod
    def _resolve_lock(trade: Trade, future: pd.DataFrame, trigger_r: float, lock_r: float) -> Trade:
        t = replace(trade, outcome="OPEN", exit_price=None, exit_time=None, mfe_r=0.0, mae_r=0.0, intrabar_ambiguous=False)
        risk = t.risk
        active_stop = t.stop
        pending_stop: float | None = None
        max_fav = max_adv = 0.0
        for _, row in future.iterrows():
            if pending_stop is not None:
                active_stop, pending_stop = pending_stop, None
            high, low = float(row["high"]), float(row["low"])
            if t.direction == "LONG":
                max_fav, max_adv = max(max_fav, high-t.entry), max(max_adv, t.entry-low)
                stop_hit = low <= active_stop
                trigger_hit = high >= t.entry + trigger_r*risk
                new_stop = t.entry + lock_r*risk
            else:
                max_fav, max_adv = max(max_fav, t.entry-low), max(max_adv, high-t.entry)
                stop_hit = high >= active_stop
                trigger_hit = low <= t.entry - trigger_r*risk
                new_stop = t.entry - lock_r*risk
            t.mfe_r, t.mae_r = max_fav/risk, max_adv/risk
            if stop_hit:
                t.outcome, t.exit_price, t.exit_time = "EXIT", active_stop, row["timestamp"]
                return t
            if trigger_hit:
                better = new_stop > active_stop if t.direction == "LONG" else new_stop < active_stop
                if better:
                    pending_stop = new_stop
        t.outcome = "UNRESOLVED"
        if not future.empty:
            t.exit_price, t.exit_time = float(future.iloc[-1]["close"]), future.iloc[-1]["timestamp"]
        return t

    @staticmethod
    def _resolve_ema20(trade: Trade, future: pd.DataFrame) -> Trade:
        t = replace(trade, outcome="OPEN", exit_price=None, exit_time=None, mfe_r=0.0, mae_r=0.0, intrabar_ambiguous=False)
        risk = t.risk
        max_fav = max_adv = 0.0
        for _, row in future.iterrows():
            high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
            if t.direction == "LONG":
                max_fav, max_adv = max(max_fav, high-t.entry), max(max_adv, t.entry-low)
                if low <= t.stop:
                    t.outcome, t.exit_price, t.exit_time = "SL", t.stop, row["timestamp"]
                    return t
                ema_exit = close < float(row["ema20"])
            else:
                max_fav, max_adv = max(max_fav, t.entry-low), max(max_adv, high-t.entry)
                if high >= t.stop:
                    t.outcome, t.exit_price, t.exit_time = "SL", t.stop, row["timestamp"]
                    return t
                ema_exit = close > float(row["ema20"])
            t.mfe_r, t.mae_r = max_fav/risk, max_adv/risk
            if ema_exit:
                t.outcome, t.exit_price, t.exit_time = "EMA20", close, row["timestamp"]
                return t
        t.outcome = "UNRESOLVED"
        if not future.empty:
            t.exit_price, t.exit_time = float(future.iloc[-1]["close"]), future.iloc[-1]["timestamp"]
        return t

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

    @staticmethod
    def _realized_r(trade: Trade) -> float:
        if trade.exit_price is None or trade.risk <= 0:
            return 0.0
        return ((trade.exit_price-trade.entry)/trade.risk) if trade.direction == "LONG" else ((trade.entry-trade.exit_price)/trade.risk)

    def _stats(self, trades: list[Trade]) -> dict[str, Any]:
        resolved = [t for t in trades if t.outcome not in {"UNRESOLVED","AMBIGUOUS","OPEN"} and t.exit_price is not None]
        net_values = [self._net_eur(t) for t in resolved]
        net_wins = [v for v in net_values if v > 0]
        net_losses = [-v for v in net_values if v < 0]
        r_values = [self._realized_r(t) for t in resolved]
        technical_wins = [v for v in r_values if v > 0]
        technical_losses = [-v for v in r_values if v < 0]
        equity, peak, max_dd = 100.0, 100.0, 0.0
        for t in sorted(resolved, key=lambda x: x.exit_time):
            equity += self._net_eur(t)
            peak = max(peak, equity)
            max_dd = max(max_dd, peak-equity)
        return {
            "resolved": len(resolved),
            "wins": len(technical_wins),
            "losses": len(technical_losses),
            "win_rate": len(technical_wins)/len(resolved) if resolved else 0.0,
            "net_positive_rate": len(net_wins)/len(resolved) if resolved else 0.0,
            "net_eur": sum(net_values), "ending_capital": 100.0+sum(net_values),
            "pf_net_eur": (sum(net_wins)/sum(net_losses)) if net_losses else None,
            "pf_gross_r": (sum(technical_wins)/sum(technical_losses)) if technical_losses else None,
            "expectancy_eur": sum(net_values)/len(resolved) if resolved else 0.0,
            "expectancy_r": sum(r_values)/len(resolved) if resolved else 0.0,
            "max_drawdown_eur": max_dd,
            "avg_win_eur": sum(net_wins)/len(net_wins) if net_wins else 0.0,
            "avg_loss_eur": sum(net_losses)/len(net_losses) if net_losses else 0.0,
            "intrabar_ambiguous": sum(1 for t in trades if t.intrabar_ambiguous),
        }

    def run(self) -> dict[str, Any]:
        self.logger.info("V1_HISTORICAL_AUDIT_START")
        base_signals: list[Trade] = []
        coverage: list[dict[str, Any]] = []
        frames: dict[str,pd.DataFrame] = {}
        for pair in SYMBOLS:
            trades, cov = self._signals_for_pair(pair)
            coverage.append(cov)
            frames[pair] = self._indicators(self._frame(pair,"15m"))
            base_signals.extend(trades)

        def future(t: Trade) -> pd.DataFrame:
            return frames[t.pair][frames[t.pair]["timestamp"] > t.time]

        # Original audit preserves unresolved intrabar ordering as AMBIGUOUS.
        original = [self._resolve_fixed(t, future(t), 1.5, stop_first=False) for t in base_signals]
        original_resolved = [t for t in original if t.outcome in {"TP","SL"}]
        original_stats = self._stats(original)
        original_stats.update({
            "signals": len(original),
            "ambiguous": sum(t.outcome=="AMBIGUOUS" for t in original),
            "unresolved": sum(t.outcome=="UNRESOLVED" for t in original),
            "tp2_touched": sum(t.tp2_touched for t in original),
            "avg_mfe_r": sum(t.mfe_r for t in original)/len(original) if original else 0.0,
            "avg_mae_r": sum(t.mae_r for t in original)/len(original) if original else 0.0,
            "avg_risk_pct_wins": sum(t.risk_pct for t in original_resolved if t.outcome=="TP")/max(1,sum(t.outcome=="TP" for t in original_resolved)),
            "avg_risk_pct_losses": sum(t.risk_pct for t in original_resolved if t.outcome=="SL")/max(1,sum(t.outcome=="SL" for t in original_resolved)),
        })

        # Exit comparisons use conservative STOP-FIRST when both levels are touched
        # inside the same 15m candle. This avoids discarding more low-target trades
        # merely because OHLC data cannot reveal the intrabar sequence.
        fixed: dict[str, dict[str, Any]] = {}
        for target_r in (0.50,0.75,1.00,1.25,1.50,2.00):
            simulated = [self._resolve_fixed(t, future(t), target_r, stop_first=True) for t in base_signals]
            fixed[f"{target_r:.2f}R"] = self._stats(simulated)

        locks = {}
        for trigger_r, lock_r in ((0.50,0.00),(0.75,0.25),(1.00,0.50),(1.50,1.00)):
            locks[f"{trigger_r:.2f}->{lock_r:.2f}R"] = self._stats([self._resolve_lock(t, future(t), trigger_r, lock_r) for t in base_signals])

        ema20 = self._stats([self._resolve_ema20(t, future(t)) for t in base_signals])
        summary = {**original_stats, "coverage": coverage, "fixed_targets": fixed, "locks": locks, "ema20_exit": ema20}
        self.logger.warning("V1_HISTORICAL_AUDIT_SUMMARY %s", summary)
        return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        pf_eur = s.get("pf_net_eur")
        pf_r = s.get("pf_gross_r")
        def pf(v: Any) -> str:
            return "n/d" if v is None else f"{v:.2f}"
        fixed_lines = []
        for label, x in s.get("fixed_targets",{}).items():
            fixed_lines.append(
                f"• TP {label}: WR tecnico <b>{100*x['win_rate']:.1f}%</b> · "
                f"net+ <b>{100*x.get('net_positive_rate',0):.1f}%</b> · PF netto <b>{pf(x['pf_net_eur'])}</b> · "
                f"P&L <b>€{x['net_eur']:+.2f}</b> · ambigui stop-first <b>{x.get('intrabar_ambiguous',0)}</b>"
            )
        lock_lines = []
        for label, x in s.get("locks",{}).items():
            lock_lines.append(
                f"• Protezione {label}: WR tecnico <b>{100*x['win_rate']:.1f}%</b> · "
                f"PF netto <b>{pf(x['pf_net_eur'])}</b> · P&L <b>€{x['net_eur']:+.2f}</b>"
            )
        ema = s.get("ema20_exit",{})
        return (
            "📊 <b>AUDIT V1 · ECONOMIA E USCITE · CORRETTO</b>\n"
            f"Segnali: <b>{s.get('signals',0)}</b> · risolti originali: <b>{s.get('resolved',0)}</b>\n"
            f"Win rate tecnico TP1 1,5R: <b>{100*s.get('win_rate',0):.1f}%</b>\n"
            f"Trade net-positive dopo costi: <b>{100*s.get('net_positive_rate',0):.1f}%</b>\n"
            f"P&L netto originale: <b>€{s.get('net_eur',0):+.2f}</b>\n"
            f"PF netto in euro: <b>{pf(pf_eur)}</b> · PF in R: <b>{pf(pf_r)}</b>\n"
            f"Expectancy: <b>€{s.get('expectancy_eur',0):+.3f}</b> · <b>{s.get('expectancy_r',0):+.3f}R</b>/trade\n"
            f"Vincita netta media: <b>€{s.get('avg_win_eur',0):.3f}</b> · perdita netta media: <b>€{s.get('avg_loss_eur',0):.3f}</b>\n"
            f"Rischio % medio winner: <b>{100*s.get('avg_risk_pct_wins',0):.2f}%</b> · loser: <b>{100*s.get('avg_risk_pct_losses',0):.2f}%</b>\n"
            f"MFE medio: <b>+{s.get('avg_mfe_r',0):.2f}R</b> · MAE: <b>-{s.get('avg_mae_r',0):.2f}R</b>\n\n"
            "🎯 <b>Target fissi · stessi ingressi · stop-first conservativo</b>\n" + "\n".join(fixed_lines) +
            "\n\n🛡 <b>Protezioni progressive</b>\n" + "\n".join(lock_lines) +
            f"\n\n📉 <b>Uscita EMA20</b>: WR tecnico <b>{100*ema.get('win_rate',0):.1f}%</b> · "
            f"PF netto <b>{pf(ema.get('pf_net_eur'))}</b> · P&L <b>€{ema.get('net_eur',0):+.2f}</b>\n"
            "\n⚠️ WR = esito tecnico lordo in R. PF/P&L = dopo costi. Nei target fissi, doppio tocco SL+TP nella stessa 15m è conteggiato conservativamente come SL e segnalato a parte."
        )
