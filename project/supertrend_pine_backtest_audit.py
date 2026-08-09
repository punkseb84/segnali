"""One-shot historical backtest of the user-provided Pine Supertrend script.

Pine logic reproduced:
    [supertrend, direction] = ta.supertrend(3.0, 10)
    BUY  = direction < 0 and direction[1] > 0
    SELL = direction > 0 and direction[1] < 0

The indicator itself does not define trade execution. To avoid look-ahead, this
audit executes every signal at the NEXT 15m bar open. Two interpretations are
reported:
  1) LONG_ONLY: BUY opens long, SELL closes long.
  2) REVERSAL: BUY opens/reverses long, SELL opens/reverses short.
Market/taker fees, spread and slippage are included in net results.
No live rules are changed.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any
import json
import math
import statistics

import pandas as pd

from project.velez_candidate_audit import VelezCandidateAudit

AUDIT_VERSION = "PINE_SUPERTREND_10_3_BACKTEST_V1_20260809"
ATR_LENGTH = 10
FACTOR = 3.0
TIMEFRAME = "15m"


class SupertrendPineBacktestAudit(VelezCandidateAudit):
    def __init__(
        self,
        *args: Any,
        notional_eur: float = 25.0,
        buy_fee_rate: float = 0.0016,
        sell_fee_rate: float = 0.0016,
        spread_rate: float = 0.0005,
        slippage_rate: float = 0.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.notional = float(notional_eur)
        self.buy_fee = float(buy_fee_rate)
        self.sell_fee = float(sell_fee_rate)
        self.spread = float(spread_rate)
        self.slippage = float(slippage_rate)

    def already_done(self) -> bool:
        self._ensure_marker()
        return bool(self.postgres.fetch_all(
            "SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1",
            (AUDIT_VERSION,),
        ))

    def _save_marker(self, summary: dict[str, Any]) -> None:
        self.postgres.execute(
            "INSERT INTO statistics.velez_candidate_audit_runs(version,summary) VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING",
            (AUDIT_VERSION, json.dumps(summary, default=str)),
        )

    @staticmethod
    def _rma_true_range(df: pd.DataFrame, length: int) -> list[float]:
        high = df["high"].astype(float).to_list()
        low = df["low"].astype(float).to_list()
        close = df["close"].astype(float).to_list()
        tr: list[float] = []
        for i in range(len(df)):
            if i == 0:
                tr.append(high[i] - low[i])
            else:
                tr.append(max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1])))
        atr = [math.nan] * len(df)
        if len(df) < length:
            return atr
        atr[length-1] = sum(tr[:length]) / length
        for i in range(length, len(df)):
            atr[i] = (atr[i-1] * (length - 1) + tr[i]) / length
        return atr

    @classmethod
    def _supertrend_direction(cls, df: pd.DataFrame) -> list[float]:
        """TradingView-compatible ta.supertrend direction: -1 uptrend, +1 downtrend."""
        n = len(df)
        atr = cls._rma_true_range(df, ATR_LENGTH)
        high = df["high"].astype(float).to_list()
        low = df["low"].astype(float).to_list()
        close = df["close"].astype(float).to_list()
        upper = [math.nan] * n
        lower = [math.nan] * n
        st = [math.nan] * n
        direction = [math.nan] * n

        for i in range(n):
            if math.isnan(atr[i]):
                continue
            hl2 = (high[i] + low[i]) / 2.0
            basic_upper = hl2 + FACTOR * atr[i]
            basic_lower = hl2 - FACTOR * atr[i]

            if i == 0 or math.isnan(upper[i-1]):
                upper[i] = basic_upper
                lower[i] = basic_lower
            else:
                prev_upper = upper[i-1]
                prev_lower = lower[i-1]
                prev_close = close[i-1]
                lower[i] = basic_lower if (basic_lower > prev_lower or prev_close < prev_lower) else prev_lower
                upper[i] = basic_upper if (basic_upper < prev_upper or prev_close > prev_upper) else prev_upper

            # Pine reference implementation initializes direction=+1 while prior ATR is na.
            if i == 0 or i-1 < 0 or math.isnan(atr[i-1]) or math.isnan(st[i-1]):
                direction[i] = 1.0
            elif abs(st[i-1] - upper[i-1]) <= max(1e-12, abs(st[i-1]) * 1e-12):
                direction[i] = -1.0 if close[i] > upper[i] else 1.0
            else:
                direction[i] = 1.0 if close[i] < lower[i] else -1.0
            st[i] = lower[i] if direction[i] < 0 else upper[i]
        return direction

    def _trade_pnl(self, direction: str, entry: float, exit_price: float) -> tuple[float, float]:
        if entry <= 0 or exit_price <= 0:
            return 0.0, 0.0
        qty = self.notional / entry
        gross = qty * ((exit_price-entry) if direction == "LONG" else (entry-exit_price))
        # Treat every executed entry/exit as market/taker. Half-spread on each side.
        entry_fee = self.notional * self.buy_fee
        exit_notional = qty * exit_price
        exit_fee = exit_notional * self.sell_fee
        friction = (self.notional + exit_notional) * (self.spread / 2.0 + self.slippage)
        return gross, gross - entry_fee - exit_fee - friction

    @staticmethod
    def _stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
        vals = [float(t["net"]) for t in trades]
        gross_vals = [float(t["gross"]) for t in trades]
        wins = [v for v in vals if v > 0]
        losses = [v for v in vals if v < 0]
        gp = sum(wins)
        gl = abs(sum(losses))
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in sorted(trades, key=lambda x: x["exit_time"]):
            equity += float(t["net"])
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return {
            "n": len(trades),
            "wr": 100.0 * len(wins) / len(trades) if trades else 0.0,
            "gross": sum(gross_vals),
            "net": sum(vals),
            "pf": gp/gl if gl else (999.0 if gp else 0.0),
            "exp": statistics.mean(vals) if vals else 0.0,
            "avg_gross": statistics.mean(gross_vals) if gross_vals else 0.0,
            "max_dd": max_dd,
            "avg_bars": statistics.mean([t["bars"] for t in trades]) if trades else 0.0,
        }

    def _pair_trades(self, pair: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        d = self._frame(pair).copy()
        if len(d) < ATR_LENGTH + 5:
            return [], [], {"pair": pair, "bars": len(d), "buy_signals": 0, "sell_signals": 0}
        d = d.sort_values("timestamp").reset_index(drop=True)
        direction = self._supertrend_direction(d)
        signals: list[tuple[int, str]] = []
        for i in range(1, len(d)-1):  # need next bar open for execution
            a, b = direction[i-1], direction[i]
            if math.isnan(a) or math.isnan(b):
                continue
            if b < 0 and a > 0:
                signals.append((i, "BUY"))
            elif b > 0 and a < 0:
                signals.append((i, "SELL"))
        buy_count = sum(s == "BUY" for _, s in signals)
        sell_count = sum(s == "SELL" for _, s in signals)

        # LONG-only: BUY -> next open entry; SELL -> next open exit.
        long_trades: list[dict[str, Any]] = []
        open_long: dict[str, Any] | None = None
        for sig_i, sig in signals:
            exec_i = sig_i + 1
            px = float(d.iloc[exec_i]["open"])
            tm = d.iloc[exec_i]["timestamp"]
            if sig == "BUY" and open_long is None:
                open_long = {"entry": px, "entry_i": exec_i, "entry_time": tm}
            elif sig == "SELL" and open_long is not None:
                gross, net = self._trade_pnl("LONG", open_long["entry"], px)
                long_trades.append({"pair":pair,"direction":"LONG","entry":open_long["entry"],"exit":px,"entry_time":open_long["entry_time"],"exit_time":tm,"bars":exec_i-open_long["entry_i"],"gross":gross,"net":net})
                open_long = None

        # REVERSAL: each opposite signal closes current and opens new direction at next open.
        rev_trades: list[dict[str, Any]] = []
        pos: dict[str, Any] | None = None
        for sig_i, sig in signals:
            exec_i = sig_i + 1
            px = float(d.iloc[exec_i]["open"])
            tm = d.iloc[exec_i]["timestamp"]
            new_dir = "LONG" if sig == "BUY" else "SHORT"
            if pos is None:
                pos = {"direction":new_dir,"entry":px,"entry_i":exec_i,"entry_time":tm}
                continue
            if pos["direction"] == new_dir:
                continue
            gross, net = self._trade_pnl(pos["direction"], pos["entry"], px)
            rev_trades.append({"pair":pair,"direction":pos["direction"],"entry":pos["entry"],"exit":px,"entry_time":pos["entry_time"],"exit_time":tm,"bars":exec_i-pos["entry_i"],"gross":gross,"net":net})
            pos = {"direction":new_dir,"entry":px,"entry_i":exec_i,"entry_time":tm}

        info = {
            "pair": pair,
            "bars": len(d),
            "from": str(d.iloc[0]["timestamp"]),
            "to": str(d.iloc[-1]["timestamp"]),
            "buy_signals": buy_count,
            "sell_signals": sell_count,
        }
        return long_trades, rev_trades, info

    def run(self) -> dict[str, Any]:
        if self.already_done():
            return {"skipped": True, "version": AUDIT_VERSION}
        all_long: list[dict[str, Any]] = []
        all_rev: list[dict[str, Any]] = []
        coverage: list[dict[str, Any]] = []
        pair_results: dict[str, Any] = {}
        for pair in self.pairs[:20]:
            lt, rt, info = self._pair_trades(pair)
            all_long.extend(lt)
            all_rev.extend(rt)
            coverage.append(info)
            pair_results[pair] = {"long": self._stats(lt), "reversal": self._stats(rt), "coverage": info}
        summary = {
            "version": AUDIT_VERSION,
            "timeframe": TIMEFRAME,
            "atr_length": ATR_LENGTH,
            "factor": FACTOR,
            "pairs": list(self.pairs[:20]),
            "notional": self.notional,
            "buy_fee": self.buy_fee,
            "sell_fee": self.sell_fee,
            "spread": self.spread,
            "slippage": self.slippage,
            "execution": "NEXT_BAR_OPEN",
            "long": self._stats(all_long),
            "reversal": self._stats(all_rev),
            "pair_results": pair_results,
            "coverage": coverage,
        }
        self._save_marker(summary)
        return summary

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        if s.get("skipped"):
            return ""
        l=s.get("long",{});r=s.get("reversal",{})
        lines=[
            "📈 <b>BACKTEST PINE · SUPERTREND 10×3</b>",
            f"Timeframe: <b>{s.get('timeframe','15m')}</b> · crypto: <b>{len(s.get('pairs',[]))}</b> · esecuzione: <b>open barra successiva</b>",
            f"Capitale simulato/trade: <b>€{s.get('notional',0):.2f}</b>",
            f"Costi: fee entrata {100*s.get('buy_fee',0):.3f}% · fee uscita {100*s.get('sell_fee',0):.3f}% · spread {100*s.get('spread',0):.3f}%",
            "",
            "🟢 <b>Interpretazione LONG-only</b>",
            "BUY = entra LONG · SELL = chiude",
            f"Trade: <b>{l.get('n',0)}</b> · WR netto <b>{l.get('wr',0):.1f}%</b> · PF netto <b>{l.get('pf',0):.2f}</b>",
            f"P&L lordo: <b>€{l.get('gross',0):+.2f}</b> · P&L netto: <b>€{l.get('net',0):+.2f}</b> · Exp: <b>€{l.get('exp',0):+.3f}/trade</b>",
            f"Max DD sequenziale: <b>€{l.get('max_dd',0):.2f}</b> · durata media: <b>{l.get('avg_bars',0):.1f} barre</b>",
            "",
            "🔄 <b>Interpretazione REVERSAL LONG/SHORT</b>",
            "BUY = LONG · SELL = SHORT · ogni flip chiude e gira",
            f"Trade: <b>{r.get('n',0)}</b> · WR netto <b>{r.get('wr',0):.1f}%</b> · PF netto <b>{r.get('pf',0):.2f}</b>",
            f"P&L lordo: <b>€{r.get('gross',0):+.2f}</b> · P&L netto: <b>€{r.get('net',0):+.2f}</b> · Exp: <b>€{r.get('exp',0):+.3f}/trade</b>",
            f"Max DD sequenziale: <b>€{r.get('max_dd',0):.2f}</b> · durata media: <b>{r.get('avg_bars',0):.1f} barre</b>",
            "",
            "📦 <b>Risultato LONG-only per coppia</b>",
        ]
        prs=s.get("pair_results",{})
        ordered=sorted(prs.items(),key=lambda kv:kv[1]["long"]["net"],reverse=True)
        for pair,x in ordered:
            st=x["long"]
            lines.append(f"• {pair}: n={st['n']} · WR {st['wr']:.1f}% · PF {st['pf']:.2f} · netto €{st['net']:+.2f}")
        lines += [
            "",
            "⚠️ Il Pine originale è un indicatore, non definisce sizing/ordini. Questo backtest usa €25 per trade e next-bar-open per evitare look-ahead; nessuna modifica al bot live.",
        ]
        return "\n".join(lines)
