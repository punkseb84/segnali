"""One-shot execution-timing audit for the user-provided Pine Supertrend 10x3.

Uses the exact same confirmed BUY/SELL flips and compares:
- SIGNAL_CLOSE: execute at close of the confirmed signal candle.
- NEXT_BAR_OPEN: execute at open of the following 15m candle.

Reports reversal trades overall plus LONG and SHORT legs separately. Market costs
are unchanged. Diagnostic only; no live changes.
"""
from __future__ import annotations

from typing import Any
import json
import math

from project.supertrend_pine_backtest_audit import SupertrendPineBacktestAudit

AUDIT_VERSION = "PINE_SUPERTREND_10_3_EXECUTION_TIMING_V1_20260809"


class SupertrendExecutionTimingAudit(SupertrendPineBacktestAudit):
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

    def _signals(self, pair: str):
        d = self._frame(pair).copy()
        if len(d) < 15:
            return d, []
        d = d.sort_values("timestamp").reset_index(drop=True)
        direction = self._supertrend_direction(d)
        signals = []
        for i in range(1, len(d) - 1):
            a, b = direction[i - 1], direction[i]
            if math.isnan(a) or math.isnan(b):
                continue
            if b < 0 and a > 0:
                signals.append((i, "BUY"))
            elif b > 0 and a < 0:
                signals.append((i, "SELL"))
        return d, signals

    def _reversal_for_mode(self, pair: str, mode: str) -> list[dict[str, Any]]:
        d, signals = self._signals(pair)
        trades: list[dict[str, Any]] = []
        pos: dict[str, Any] | None = None
        for sig_i, sig in signals:
            j = sig_i if mode == "SIGNAL_CLOSE" else sig_i + 1
            if j >= len(d):
                continue
            px = float(d.iloc[j]["close"] if mode == "SIGNAL_CLOSE" else d.iloc[j]["open"])
            tm = d.iloc[j]["timestamp"]
            nd = "LONG" if sig == "BUY" else "SHORT"
            if pos is None:
                pos = {"direction": nd, "entry": px, "entry_i": j, "entry_time": tm}
                continue
            if pos["direction"] == nd:
                continue
            gross, net = self._trade_pnl(pos["direction"], pos["entry"], px)
            trades.append({
                "pair": pair,
                "direction": pos["direction"],
                "entry": pos["entry"],
                "exit": px,
                "entry_time": pos["entry_time"],
                "exit_time": tm,
                "bars": max(0, j - pos["entry_i"]),
                "gross": gross,
                "net": net,
            })
            pos = {"direction": nd, "entry": px, "entry_i": j, "entry_time": tm}
        return trades

    def run(self) -> dict[str, Any]:
        if self.already_done():
            return {"skipped": True, "version": AUDIT_VERSION}
        close_trades: list[dict[str, Any]] = []
        open_trades: list[dict[str, Any]] = []
        pair_results: dict[str, Any] = {}
        for pair in self.pairs[:20]:
            ct = self._reversal_for_mode(pair, "SIGNAL_CLOSE")
            ot = self._reversal_for_mode(pair, "NEXT_BAR_OPEN")
            close_trades.extend(ct)
            open_trades.extend(ot)
            pair_results[pair] = {
                "close": self._stats(ct),
                "open": self._stats(ot),
            }

        def split(trades: list[dict[str, Any]], direction: str):
            return [t for t in trades if t["direction"] == direction]

        close_stats = self._stats(close_trades)
        open_stats = self._stats(open_trades)
        s = {
            "version": AUDIT_VERSION,
            "pairs": list(self.pairs[:20]),
            "close": close_stats,
            "open": open_stats,
            "close_long": self._stats(split(close_trades, "LONG")),
            "close_short": self._stats(split(close_trades, "SHORT")),
            "open_long": self._stats(split(open_trades, "LONG")),
            "open_short": self._stats(split(open_trades, "SHORT")),
            "pair_results": pair_results,
            "delta_net": close_stats["net"] - open_stats["net"],
            "delta_exp": close_stats["exp"] - open_stats["exp"],
            "notional": self.notional,
            "buy_fee": self.buy_fee,
            "sell_fee": self.sell_fee,
            "spread": self.spread,
        }
        self._save_marker(s)
        return s

    @staticmethod
    def format_report(s: dict[str, Any]) -> str:
        if s.get("skipped"):
            return ""
        c = s.get("close", {})
        o = s.get("open", {})
        cl = s.get("close_long", {})
        cs = s.get("close_short", {})
        ol = s.get("open_long", {})
        os = s.get("open_short", {})
        lines = [
            "⏱ <b>SUPERTREND 10×3 · PREZZO DI ESECUZIONE</b>",
            f"20 crypto · 15m · €{s.get('notional',25):.2f}/trade · stessi flip BUY/SELL",
            f"Costi invariati: fee {100*s.get('buy_fee',0):.3f}% + {100*s.get('sell_fee',0):.3f}% · spread {100*s.get('spread',0):.3f}%",
            "",
            "🕯 <b>Close della candela del segnale</b>",
            f"Tutti: n={c.get('n',0)} · WR {c.get('wr',0):.1f}% · PF {c.get('pf',0):.2f} · lordo €{c.get('gross',0):+.2f} · netto €{c.get('net',0):+.2f} · Exp €{c.get('exp',0):+.3f}",
            f"LONG BUY→SELL: n={cl.get('n',0)} · PF {cl.get('pf',0):.2f} · netto €{cl.get('net',0):+.2f} · Exp €{cl.get('exp',0):+.3f}",
            f"SHORT SELL→BUY: n={cs.get('n',0)} · PF {cs.get('pf',0):.2f} · netto €{cs.get('net',0):+.2f} · Exp €{cs.get('exp',0):+.3f}",
            "",
            "➡️ <b>Open della candela successiva</b>",
            f"Tutti: n={o.get('n',0)} · WR {o.get('wr',0):.1f}% · PF {o.get('pf',0):.2f} · lordo €{o.get('gross',0):+.2f} · netto €{o.get('net',0):+.2f} · Exp €{o.get('exp',0):+.3f}",
            f"LONG BUY→SELL: n={ol.get('n',0)} · PF {ol.get('pf',0):.2f} · netto €{ol.get('net',0):+.2f} · Exp €{ol.get('exp',0):+.3f}",
            f"SHORT SELL→BUY: n={os.get('n',0)} · PF {os.get('pf',0):.2f} · netto €{os.get('net',0):+.2f} · Exp €{os.get('exp',0):+.3f}",
            "",
            f"Differenza close − next open: P&L €{s.get('delta_net',0):+.2f} · Exp €{s.get('delta_exp',0):+.3f}/trade",
            "",
            "⚠️ Il close del segnale è uno scenario più ottimistico: l’etichetta è confermata solo a candela chiusa. Il confronto serve a misurare quanto pesa il prezzo di esecuzione; nessuna modifica al live.",
        ]
        return "\n".join(lines)
