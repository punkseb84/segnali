"""Directional BUY/SELL extension of the daily 13:00 PAPER scanner.

SELL is a synthetic bearish PAPER trade: entry at bid, stop above entry,
take-profit below entry, and P&L increases when price falls.  BUY remains the
symmetric bullish case.  The five daily signals are the five highest-scoring
setups regardless of direction.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from html import escape
import json
import math
from typing import Any

import pandas as pd

from project.daily_coinbase_runtime import (
    ANALYSIS_HOUR,
    COINBASE_TAKER_FEE,
    DAILY_BUDGET_EUR,
    MONITOR_SECONDS,
    SIGNAL_COUNT,
    TRADE_NOTIONAL_EUR,
    UNIVERSE_SIZE,
    DailyCoinbaseRuntime,
)


@dataclass
class DirectionalCandidate:
    product_id: str
    direction: str
    score: float
    setup: str
    entry: float
    stop: float
    target: float
    net_tp_pct: float
    net_sl_pct: float
    net_rr: float
    reason: str


class DailyCoinbaseBuySellRuntime(DailyCoinbaseRuntime):
    def _ensure_schema(self) -> None:
        super()._ensure_schema()
        self.postgres.execute(
            "ALTER TABLE daily_coinbase.trades ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'BUY'"
        )

    @staticmethod
    def _net_directional_return(entry: float, exit_price: float, direction: str) -> float:
        if entry <= 0 or exit_price <= 0:
            return 0.0
        d = str(direction).upper()
        if d == "SELL":
            # Synthetic short: receive sale proceeds at entry after fee, buy
            # back at exit including fee. Positive if exit is sufficiently lower.
            return (entry * (1.0 - COINBASE_TAKER_FEE)) / (
                exit_price * (1.0 + COINBASE_TAKER_FEE)
            ) - 1.0
        return (exit_price * (1.0 - COINBASE_TAKER_FEE)) / (
            entry * (1.0 + COINBASE_TAKER_FEE)
        ) - 1.0

    @staticmethod
    def _target_price_for_net_directional(entry: float, target_net: float, direction: str) -> float:
        if str(direction).upper() == "SELL":
            return entry * (1.0 - COINBASE_TAKER_FEE) / (
                (1.0 + target_net) * (1.0 + COINBASE_TAKER_FEE)
            )
        return entry * (1.0 + COINBASE_TAKER_FEE) * (1.0 + target_net) / (
            1.0 - COINBASE_TAKER_FEE
        )

    def _score_directional_candidate(
        self,
        product_id: str,
        x15: pd.DataFrame,
        x1h: pd.DataFrame,
        btc1h: pd.DataFrame,
        liquidity_rank: float,
    ) -> DirectionalCandidate | None:
        if len(x15) < 220 or len(x1h) < 220 or len(btc1h) < 30:
            return None
        x30 = self._resample_30m(x15)
        if len(x30) < 80:
            return None

        f15, f30, f1 = self._features(x15), self._features(x30), self._features(x1h)
        btc = self._features(btc1h)
        required = [f15["close"], f15["atr"], f30["rsi"], f1["ema200"], f1["rsi"]]
        if not all(math.isfinite(float(v)) for v in required):
            return None

        rs4 = f1["ret4"] - btc["ret4"]
        rs24 = f1["ret24"] - btc["ret24"]
        atr_pct = f15["atr"] / max(f15["close"], 1e-12)
        liq_bonus = 5 if liquidity_rank <= 10 else 3 if liquidity_rank <= 20 else 1

        prev20_low = float(x15["low"].astype(float).rolling(20).min().shift(1).iloc[-1])
        breakout_up = f15["close"] > f15["prev20_high"] and f15["close"] > f15["open"]
        breakout_down = f15["close"] < prev20_low and f15["close"] < f15["open"]

        # BUY trend/momentum score.
        buy = float(liq_bonus)
        buy_reasons: list[str] = []
        if f1["close"] > f1["ema20"]: buy += 8
        if f1["ema20"] > f1["ema50"]: buy += 8
        if f1["ema50"] > f1["ema200"]:
            buy += 10
            buy_reasons.append("struttura 1h rialzista sopra EMA20/50/200")
        if f1["ema20"] > f1["ema20_prev4"]: buy += 5
        if 50 <= f1["rsi"] <= 70:
            buy += 6
            buy_reasons.append(f"RSI 1h {f1['rsi']:.0f} in zona momentum")
        if f1["macd"] > 0 and f1["macd"] > f1["macd_prev"]: buy += 6
        if f30["close"] > f30["ema20"]: buy += 5
        if 50 <= f30["rsi"] <= 68: buy += 6
        if f30["macd"] > 0:
            buy += 5
            buy_reasons.append("momentum 30m positivo")
        if f30["ret4"] > 0: buy += 3
        pullback_buy = (
            f1["ema20"] > f1["ema50"]
            and f15["close"] > f15["ema50"]
            and abs(f15["close"] - f15["ema20"]) / max(f15["close"], 1e-12) <= 0.007
            and f15["close"] > f15["open"]
        )
        if breakout_up:
            buy += 10
            buy_reasons.append("breakout 15m dei massimi recenti")
        elif pullback_buy:
            buy += 8
            buy_reasons.append("pullback 15m su EMA20 con reazione rialzista")
        elif f15["close"] > f15["ema20"]:
            buy += 3
        if f15["rvol"] >= 1.5:
            buy += 7
            buy_reasons.append(f"volume 15m {f15['rvol']:.1f}× la media")
        elif f15["rvol"] >= 1.1:
            buy += 4
        if 48 <= f15["rsi"] <= 68: buy += 5
        if f15["macd"] > 0 and f15["macd"] > f15["macd_prev"]: buy += 5
        if rs4 >= 0.005:
            buy += 8
            buy_reasons.append(f"forza relativa vs BTC +{100*rs4:.1f}% su 4h")
        elif rs4 > 0: buy += 4
        if rs24 >= 0.01: buy += 7
        elif rs24 > 0: buy += 3
        if 0.003 <= atr_pct <= 0.025: buy += 5
        buy_extension = f1["close"] / max(f1["ema20"], 1e-12) - 1.0
        if 0 <= buy_extension <= 0.035: buy += 4
        elif buy_extension > 0.06: buy -= 8

        # BUY mean-reversion score.
        buy_revert = float(liq_bonus)
        buy_revert_reasons: list[str] = []
        if 28 <= f1["rsi"] <= 43:
            buy_revert += 18
            buy_revert_reasons.append(f"RSI 1h depresso ({f1['rsi']:.0f})")
        if f1["z20"] <= -1.5:
            buy_revert += 15
            buy_revert_reasons.append("deviazione 1h estrema sotto la media")
        if f15["rsi_prev"] < 38 and f15["rsi"] > f15["rsi_prev"]:
            buy_revert += 14
            buy_revert_reasons.append("RSI 15m in recupero da ipervenduto")
        if f15["close"] > f15["open"] and f15["macd"] > f15["macd_prev"]: buy_revert += 12
        if f15["rvol"] >= 1.5: buy_revert += 8
        if rs4 <= -0.015 and f15["ret4"] > -0.005:
            buy_revert += 12
            buy_revert_reasons.append("sottoperformance vs BTC con stabilizzazione 15m")
        if f30["rsi"] > f15["rsi"]: buy_revert += 4
        if f1["close"] > f1["ema200"]: buy_revert += 5

        # SELL trend/momentum score: exact bearish mirror of BUY logic.
        sell = float(liq_bonus)
        sell_reasons: list[str] = []
        if f1["close"] < f1["ema20"]: sell += 8
        if f1["ema20"] < f1["ema50"]: sell += 8
        if f1["ema50"] < f1["ema200"]:
            sell += 10
            sell_reasons.append("struttura 1h ribassista sotto EMA20/50/200")
        if f1["ema20"] < f1["ema20_prev4"]: sell += 5
        if 30 <= f1["rsi"] <= 50:
            sell += 6
            sell_reasons.append(f"RSI 1h {f1['rsi']:.0f} in zona momentum ribassista")
        if f1["macd"] < 0 and f1["macd"] < f1["macd_prev"]: sell += 6
        if f30["close"] < f30["ema20"]: sell += 5
        if 32 <= f30["rsi"] <= 50: sell += 6
        if f30["macd"] < 0:
            sell += 5
            sell_reasons.append("momentum 30m negativo")
        if f30["ret4"] < 0: sell += 3
        pullback_sell = (
            f1["ema20"] < f1["ema50"]
            and f15["close"] < f15["ema50"]
            and abs(f15["close"] - f15["ema20"]) / max(f15["close"], 1e-12) <= 0.007
            and f15["close"] < f15["open"]
        )
        if breakout_down:
            sell += 10
            sell_reasons.append("breakdown 15m dei minimi recenti")
        elif pullback_sell:
            sell += 8
            sell_reasons.append("pullback 15m su EMA20 con reazione ribassista")
        elif f15["close"] < f15["ema20"]:
            sell += 3
        if f15["rvol"] >= 1.5:
            sell += 7
            sell_reasons.append(f"volume 15m {f15['rvol']:.1f}× la media")
        elif f15["rvol"] >= 1.1:
            sell += 4
        if 32 <= f15["rsi"] <= 52: sell += 5
        if f15["macd"] < 0 and f15["macd"] < f15["macd_prev"]: sell += 5
        if rs4 <= -0.005:
            sell += 8
            sell_reasons.append(f"debolezza relativa vs BTC {100*rs4:.1f}% su 4h")
        elif rs4 < 0: sell += 4
        if rs24 <= -0.01: sell += 7
        elif rs24 < 0: sell += 3
        if 0.003 <= atr_pct <= 0.025: sell += 5
        sell_extension = f1["close"] / max(f1["ema20"], 1e-12) - 1.0
        if -0.035 <= sell_extension <= 0: sell += 4
        elif sell_extension < -0.06: sell -= 8

        # SELL mean-reversion from upside exhaustion.
        sell_revert = float(liq_bonus)
        sell_revert_reasons: list[str] = []
        if 57 <= f1["rsi"] <= 72:
            sell_revert += 18
            sell_revert_reasons.append(f"RSI 1h elevato ({f1['rsi']:.0f})")
        if f1["z20"] >= 1.5:
            sell_revert += 15
            sell_revert_reasons.append("deviazione 1h estrema sopra la media")
        if f15["rsi_prev"] > 62 and f15["rsi"] < f15["rsi_prev"]:
            sell_revert += 14
            sell_revert_reasons.append("RSI 15m in inversione da area elevata")
        if f15["close"] < f15["open"] and f15["macd"] < f15["macd_prev"]: sell_revert += 12
        if f15["rvol"] >= 1.5: sell_revert += 8
        if rs4 >= 0.015 and f15["ret4"] < 0.005:
            sell_revert += 12
            sell_revert_reasons.append("sovraperformance vs BTC con perdita di spinta 15m")
        if f30["rsi"] < f15["rsi"]: sell_revert += 4
        if f1["close"] < f1["ema200"]: sell_revert += 5

        variants = [
            (buy, "BUY", "TREND", buy_reasons),
            (buy_revert, "BUY", "REVERSION", buy_revert_reasons),
            (sell, "SELL", "TREND", sell_reasons),
            (sell_revert, "SELL", "REVERSION", sell_revert_reasons),
        ]
        score, direction, setup, reasons = max(variants, key=lambda row: row[0])

        snapshot = self._market_snapshot(product_id)
        entry = float(snapshot["ask"] if direction == "BUY" else snapshot["bid"])
        if entry <= 0:
            return None

        atr_stop_pct = 1.35 * f15["atr"] / entry
        if direction == "BUY":
            swing = float(x15["low"].astype(float).iloc[-12:].min())
            structural_pct = max(0.0, (entry - swing) / entry + 0.0015)
        else:
            swing = float(x15["high"].astype(float).iloc[-12:].max())
            structural_pct = max(0.0, (swing - entry) / entry + 0.0015)
        stop_pct = min(0.028, max(0.0065, structural_pct, atr_stop_pct))
        stop = entry * (1.0 - stop_pct) if direction == "BUY" else entry * (1.0 + stop_pct)
        net_sl = self._net_directional_return(entry, stop, direction)
        risk = abs(net_sl)
        if risk <= 0:
            return None

        atr1h_pct = float(self._atr(x1h).iloc[-1]) / entry
        expected_move = min(0.045, max(0.012, 1.35 * atr1h_pct, 2.3 * atr_pct))
        target_net = min(0.05, max(1.65 * risk, expected_move))
        target = self._target_price_for_net_directional(entry, target_net, direction)
        net_tp = self._net_directional_return(entry, target, direction)
        net_rr = net_tp / risk if risk else 0.0
        if net_rr < 1.55: score -= 8
        if stop_pct >= 0.027: score -= 5

        if not reasons:
            reasons = ["migliore combinazione direzionale fra i 40 asset analizzati"]
        reason = "; ".join(reasons[:4])
        reason += (
            f". Il setup {setup.lower()} {direction} ha score {score:.0f}/100, "
            f"R/R netto {net_rr:.2f} e volatilità 15m {100*atr_pct:.2f}%."
        )
        return DirectionalCandidate(
            product_id=product_id,
            direction=direction,
            score=score,
            setup=setup,
            entry=entry,
            stop=stop,
            target=target,
            net_tp_pct=100.0 * net_tp,
            net_sl_pct=100.0 * net_sl,
            net_rr=net_rr,
            reason=reason,
        )

    def analyze_and_open_daily_batch(self, local_now: datetime) -> None:
        trade_date = local_now.date()
        if self.postgres.fetch_all(
            "SELECT 1 FROM daily_coinbase.batches WHERE trade_date=%s LIMIT 1", (trade_date,)
        ):
            return

        universe, ranks = self._available_usd_products()
        self.logger.info("DAILY_13_BUY_SELL_SCAN universe=%s", universe)
        frames: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(self._load_product_frames, pid): pid for pid in universe}
            for future in as_completed(futures):
                pid = futures[future]
                try:
                    frames[pid] = future.result()
                except Exception as exc:
                    self.logger.warning("CANDLE_LOAD_FAILED product=%s error=%s", pid, exc)

        if "BTC-USD" not in frames:
            frames["BTC-USD"] = self._load_product_frames("BTC-USD")
        btc1h = frames["BTC-USD"][1]

        candidates: list[DirectionalCandidate] = []
        for pid in universe:
            pair_frames = frames.get(pid)
            if pair_frames is None:
                continue
            try:
                c = self._score_directional_candidate(
                    pid, pair_frames[0], pair_frames[1], btc1h, ranks.get(pid, 40.0)
                )
                if c is not None:
                    candidates.append(c)
            except Exception as exc:
                self.logger.warning("DIRECTIONAL_SCORE_FAILED product=%s error=%s", pid, exc)

        candidates.sort(key=lambda c: (c.score, c.net_rr), reverse=True)
        selected = candidates[:SIGNAL_COUNT]
        if len(selected) < SIGNAL_COUNT:
            raise RuntimeError(f"INSUFFICIENT_DIRECTIONAL_SETUPS valid={len(selected)} required={SIGNAL_COUNT}")

        analysis_at = self._now_utc()
        self.postgres.execute(
            """INSERT INTO daily_coinbase.batches(
                   trade_date, analysis_at, universe, fee_rate, paper_budget_eur, status
               ) VALUES (%s,%s,%s::jsonb,%s,%s,'ACTIVE')
               ON CONFLICT (trade_date) DO NOTHING""",
            (trade_date, analysis_at, json.dumps(universe), COINBASE_TAKER_FEE, DAILY_BUDGET_EUR),
        )
        for rank, c in enumerate(selected, 1):
            self.postgres.execute(
                """INSERT INTO daily_coinbase.trades(
                       trade_date, rank, product_id, direction, setup, entry, stop_loss, take_profit,
                       score, reason, net_tp_pct, net_sl_pct, net_rr, notional_eur, status, opened_at
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'OPEN',%s)
                   ON CONFLICT (trade_date, rank) DO NOTHING""",
                (trade_date, rank, c.product_id, c.direction, c.setup, c.entry, c.stop, c.target,
                 c.score, c.reason, c.net_tp_pct, c.net_sl_pct, c.net_rr,
                 TRADE_NOTIONAL_EUR, analysis_at),
            )

        buys = sum(1 for c in selected if c.direction == "BUY")
        sells = SIGNAL_COUNT - buys
        self.send(
            "📊 <b>SCANNER CRYPTO · 13:00</b>\n"
            f"Analizzate <b>{len(universe)} crypto</b> · timeframe 15m / 30m / 1h\n"
            f"Top <b>{SIGNAL_COUNT}</b> segnali PAPER: BUY {buys} · SELL {sells}\n"
            f"€{TRADE_NOTIONAL_EUR:.2f} per segnale · costi inclusi nel R/R\n"
            "BUY guadagna sulla salita; SELL guadagna sulla discesa.\n"
            "⚠️ Selezione probabilistica, non garanzia di profitto."
        )
        for rank, c in enumerate(selected, 1):
            self.send(self._format_directional_signal(rank, c))

    def _format_directional_signal(self, rank: int, c: DirectionalCandidate) -> str:
        d = self._price_digits(c.entry)
        icon = "🟢" if c.direction == "BUY" else "🔴"
        return (
            f"{icon} <b>SEGNALE #{rank} · {escape(c.product_id)}</b>\n"
            f"Azione: <b>{c.direction}</b> · Setup: <b>{c.setup}</b>\n"
            f"Entry: <b>{c.entry:.{d}f}</b>\n"
            f"Stop Loss: <b>{c.stop:.{d}f}</b> · netto {c.net_sl_pct:+.2f}%\n"
            f"Take Profit: <b>{c.target:.{d}f}</b> · netto {c.net_tp_pct:+.2f}%\n"
            f"R/R netto: <b>{c.net_rr:.2f}</b> · Score: <b>{c.score:.0f}/100</b>\n\n"
            f"📝 <b>Perché è potenzialmente profittevole</b>\n{escape(c.reason)}"
        )

    def _open_trades(self) -> list[tuple[Any, ...]]:
        return self.postgres.fetch_all(
            """SELECT id, trade_date, rank, product_id, direction, entry, stop_loss, take_profit,
                      notional_eur, setup
               FROM daily_coinbase.trades WHERE status='OPEN'
               ORDER BY trade_date, rank"""
        )

    def _close_trade_directional(
        self,
        trade_id: int,
        entry: float,
        exit_price: float,
        direction: str,
        reason: str,
        notional: float,
        closed_at: datetime,
    ) -> tuple[float, float]:
        net_r = self._net_directional_return(entry, exit_price, direction)
        pnl = notional * net_r
        self.postgres.execute(
            """UPDATE daily_coinbase.trades
               SET status=%s, closed_at=%s, exit_price=%s,
                   net_return_pct=%s, net_pnl_eur=%s, close_reason=%s
               WHERE id=%s AND status='OPEN'""",
            (reason, closed_at, exit_price, 100.0 * net_r, pnl, reason, trade_id),
        )
        return 100.0 * net_r, pnl

    def monitor_open_trades(self) -> None:
        for row in self._open_trades():
            trade_id, trade_date, rank, pid, direction, entry, stop, target, notional, setup = row
            if trade_date < self._today_rome():
                continue
            try:
                snapshot = self._market_snapshot(str(pid))
                px = float(snapshot["bid"] if direction == "BUY" else snapshot["ask"])
            except Exception as exc:
                self.logger.warning("MONITOR_PRICE_FAILED product=%s error=%s", pid, exc)
                continue
            close_reason: str | None = None
            if direction == "BUY":
                if px <= float(stop): close_reason = "SL"
                elif px >= float(target): close_reason = "TP"
            else:
                if px >= float(stop): close_reason = "SL"
                elif px <= float(target): close_reason = "TP"
            if close_reason is None:
                continue
            net_pct, pnl = self._close_trade_directional(
                int(trade_id), float(entry), px, str(direction), close_reason,
                float(notional), self._now_utc(),
            )
            icon = "✅" if close_reason == "TP" else "❌"
            d = self._price_digits(px)
            self.send(
                f"{icon} <b>{close_reason} · {escape(str(pid))}</b>\n"
                f"Segnale #{rank} · <b>{escape(str(direction))}</b> · {escape(str(setup))}\n"
                f"Uscita: <b>{px:.{d}f}</b>\n"
                f"Risultato netto: <b>{net_pct:+.2f}% · €{pnl:+.2f}</b>"
            )

    def _fallback_exit_directional(self, product_id: str, direction: str) -> float:
        try:
            snapshot = self._market_snapshot(product_id)
            return float(snapshot["bid"] if direction == "BUY" else snapshot["ask"])
        except Exception:
            x = self._candles(product_id, 900)
            return float(x["close"].iloc[-1])

    def finalize_previous_days(self, local_now: datetime) -> None:
        today = local_now.date()
        batches = self.postgres.fetch_all(
            """SELECT trade_date FROM daily_coinbase.batches
               WHERE trade_date < %s AND recap_sent=FALSE ORDER BY trade_date""",
            (today,),
        )
        for (trade_date,) in batches:
            rows = self.postgres.fetch_all(
                """SELECT id, rank, product_id, direction, entry, notional_eur
                   FROM daily_coinbase.trades
                   WHERE trade_date=%s AND status='OPEN' ORDER BY rank""",
                (trade_date,),
            )
            for trade_id, rank, pid, direction, entry, notional in rows:
                try:
                    exit_price = self._fallback_exit_directional(str(pid), str(direction))
                    self._close_trade_directional(
                        int(trade_id), float(entry), exit_price, str(direction),
                        "MIDNIGHT", float(notional), self._now_utc(),
                    )
                except Exception as exc:
                    self.logger.exception(
                        "MIDNIGHT_CLOSE_FAILED date=%s product=%s direction=%s error=%s",
                        trade_date, pid, direction, exc,
                    )
            self._send_recap(trade_date)
            self.postgres.execute(
                """UPDATE daily_coinbase.batches
                   SET status='CLOSED', recap_sent=TRUE, closed_at=NOW()
                   WHERE trade_date=%s""", (trade_date,)
            )

    def _send_recap(self, trade_date) -> None:
        rows = self.postgres.fetch_all(
            """SELECT rank, product_id, direction, status, net_return_pct,
                      net_pnl_eur, close_reason
               FROM daily_coinbase.trades WHERE trade_date=%s ORDER BY rank""",
            (trade_date,),
        )
        total = sum(float(r[5] or 0.0) for r in rows)
        wins = sum(1 for r in rows if float(r[5] or 0.0) > 0)
        losses = sum(1 for r in rows if float(r[5] or 0.0) < 0)
        flat = len(rows) - wins - losses
        buy_n = sum(1 for r in rows if str(r[2]).upper() == "BUY")
        sell_n = len(rows) - buy_n
        lines = [
            f"🌙 <b>RESOCONTO GIORNALIERO · {trade_date:%d/%m/%Y}</b>",
            f"Operazioni: <b>{len(rows)}</b> · BUY {buy_n} · SELL {sell_n}",
            f"Positive {wins} · negative {losses} · flat {flat}", "",
        ]
        for rank, pid, direction, status, net_pct, pnl, close_reason in rows:
            value = float(pnl or 0.0)
            icon = "🟢" if value > 0 else "🔴" if value < 0 else "⚪️"
            lines.append(
                f"{icon} #{rank} <b>{escape(str(pid))}</b> · {escape(str(direction))} · "
                f"{escape(str(close_reason or status))} · {float(net_pct or 0.0):+.2f}% · €{value:+.2f}"
            )
        lines += [
            "", f"Risultato netto giornata: <b>€{total:+.2f}</b>",
            f"Budget PAPER di riferimento: €{DAILY_BUDGET_EUR:.2f} → <b>€{DAILY_BUDGET_EUR + total:.2f}</b>",
            "🔄 <b>RESET COMPLETATO</b>: nessun BUY/SELL viene trascinato al giorno successivo.",
        ]
        self.send("\n".join(lines))

    def _send_startup(self) -> None:
        self.send(
            "🤖 <b>DAILY BUY/SELL SIGNALS · ATTIVO</b>\n"
            "Ogni giorno alle <b>13:00 Europe/Rome</b>: 40 crypto, TF 15m/30m/1h, top 5 segnali BUY o SELL.\n"
            "SELL è trattato come operazione PAPER ribassista completa: entry, SL sopra e TP sotto.\n"
            "Alle <b>00:00</b>: chiusura forzata, resoconto netto e reset.\n"
            f"Costi modellati: <b>{100*COINBASE_TAKER_FEE:.3f}% per lato</b>."
        )
