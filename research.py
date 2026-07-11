"""Quant Research Engine: exhaustive strategy search with SQLite persistence and exports."""
from __future__ import annotations

import csv
import itertools
import logging
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

import pandas as pd

from config import (
    EXPORT_DIR,
    MAX_RESEARCH_RUNTIME_MINUTES,
    QUANT_PAIRS,
    QUANT_STRATEGIES,
    QUANT_TIMEFRAMES,
    RESEARCH_ADX_VALUES,
    RESEARCH_ATR_MULTIPLIERS,
    RESEARCH_BATCH_SIZE,
    RESEARCH_DB_PATH,
    RESEARCH_MARKET_REGIMES,
    RESEARCH_MIN_TRADES,
    RESEARCH_RELATIVE_VOLUME_VALUES,
    RESEARCH_REWARD_RISK_VALUES,
    RESEARCH_RSI_RANGES,
    RESUME_RESEARCH,
    TRADE_AMOUNT_EUR,
)
from indicators import add_indicators
from ohlc_cache import init_ohlc_cache, sync_ohlc_cache
from risk import calculate_plan
from strategy import closed_candles

logger = logging.getLogger("crypto-bot.research")

TP_METHOD = "ATR multiple reward/risk"
SL_METHOD = "ATR multiple stop"


@dataclass(frozen=True)
class Combination:
    strategy: str
    pair: str
    timeframe: str
    market_regime: str
    rsi_range: tuple[int, int]
    adx: int
    atr: float
    relative_volume: float
    reward_risk: float


def init_research_db() -> None:
    Path(RESEARCH_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(RESEARCH_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT, pair TEXT, timeframe TEXT, market_regime TEXT, rsi_range TEXT,
                adx REAL, atr REAL, relative_volume REAL, reward_risk REAL, tp_method TEXT, sl_method TEXT,
                number_of_trades INTEGER, win_rate REAL, profit_factor REAL, expectancy_eur REAL,
                average_win REAL, average_loss REAL, max_drawdown REAL, sharpe REAL, sortino REAL,
                net_profit REAL, training_result TEXT, validation_result TEXT, validation_profit_factor REAL,
                validation_expectancy REAL, created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS research_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER, total_combinations INTEGER, processed_combinations INTEGER,
                last_strategy TEXT, last_pair TEXT, last_timeframe TEXT, status TEXT,
                started_at TEXT, updated_at TEXT
            )
            """
        )


def total_combinations() -> int:
    return math.prod([
        len(QUANT_STRATEGIES), len(QUANT_PAIRS), len(QUANT_TIMEFRAMES), len(RESEARCH_MARKET_REGIMES),
        len(RESEARCH_RSI_RANGES), len(RESEARCH_ADX_VALUES), len(RESEARCH_ATR_MULTIPLIERS),
        len(RESEARCH_RELATIVE_VOLUME_VALUES), len(RESEARCH_REWARD_RISK_VALUES),
    ])


def generate_combinations():
    for strategy, pair, timeframe, regime, rsi, adx, atr, rv, rr in itertools.product(
        QUANT_STRATEGIES, QUANT_PAIRS, QUANT_TIMEFRAMES, RESEARCH_MARKET_REGIMES, RESEARCH_RSI_RANGES,
        RESEARCH_ADX_VALUES, RESEARCH_ATR_MULTIPLIERS, RESEARCH_RELATIVE_VOLUME_VALUES, RESEARCH_REWARD_RISK_VALUES,
    ):
        yield Combination(strategy, pair, timeframe, regime, rsi, adx, atr, rv, rr)


def _latest_progress() -> int:
    if not RESUME_RESEARCH:
        return 0
    with sqlite3.connect(RESEARCH_DB_PATH) as conn:
        row = conn.execute("SELECT processed_combinations FROM research_progress ORDER BY id DESC LIMIT 1").fetchone()
    return int(row[0]) if row else 0


def _save_progress(batch_id: int, total: int, processed: int, combo: Combination | None, status: str, started_at: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(RESEARCH_DB_PATH) as conn:
        conn.execute(
            """INSERT INTO research_progress(batch_id,total_combinations,processed_combinations,last_strategy,last_pair,last_timeframe,status,started_at,updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_id, total, processed, combo.strategy if combo else None, combo.pair if combo else None, combo.timeframe if combo else None, status, started_at, now),
        )


def _regime_mask(frame: pd.DataFrame, regime: str) -> pd.Series:
    atr_pct = (frame["atr14"] / frame["close"]).replace([math.inf, -math.inf], pd.NA)
    atr_median = atr_pct.rolling(100, min_periods=20).median()
    trend = (frame["ema20"] > frame["ema50"]) & (frame["close"] > frame["ema200"]) & (frame["adx14"] >= 20)
    if regime == "Trend":
        return trend
    if regime == "Range":
        return frame["adx14"] < 20
    if regime == "Compression":
        return atr_pct < atr_median * 0.75
    if regime == "Alta volatilità":
        return atr_pct > atr_median * 1.25
    if regime == "Bassa volatilità":
        return atr_pct < atr_median
    return pd.Series(True, index=frame.index)


def signal_mask(frame: pd.DataFrame, combo: Combination) -> pd.Series:
    low, high = combo.rsi_range
    base = frame["rsi14"].between(low, high) & (frame["adx14"] >= combo.adx) & (frame["relative_volume"] >= combo.relative_volume) & _regime_mask(frame, combo.market_regime)
    prior_high = frame["high"].rolling(20).max().shift(1)
    prior_low = frame["low"].rolling(20).min().shift(1)
    body = (frame["close"] - frame["open"]).abs()
    candle_range = (frame["high"] - frame["low"]).replace(0, pd.NA)
    compression = (frame["atr14"] / frame["close"]) < (frame["atr14"] / frame["close"]).rolling(100, min_periods=20).median() * 0.8
    rules = {
        "Breakout Retest": (frame["close"] > prior_high) & (frame["ema20"] >= frame["ema50"]),
        "Pullback Trend": (frame["low"] <= frame["ema20"]) & (frame["close"] > frame["ema20"]) & (frame["ema20"] > frame["ema50"]),
        "Liquidity Sweep": (frame["low"] < prior_low) & (frame["close"] > prior_low),
        "Range Reversal": (frame["close"] > frame["support"]) & (frame["adx14"] < 25),
        "Momentum Breakout": (frame["close"] > prior_high) & (frame["macd_hist"] > 0),
        "Compression Breakout": compression & (frame["close"] > prior_high),
        "Volatility Expansion": (body / candle_range).fillna(0) > 0.6,
        "Mean Reversion": (frame["rsi14"] < high) & (frame["close"] <= frame["support"] * 1.01),
    }
    return base & rules.get(combo.strategy, True)


def evaluate(frame: pd.DataFrame, combo: Combination, horizon_bars: int = 12) -> list[float]:
    results: list[float] = []
    indexes = frame[signal_mask(frame, combo).fillna(False)].index.tolist()
    for index in indexes:
        row = frame.loc[index]
        entry, atr = float(row.close), float(row.atr14)
        if entry <= 0 or atr <= 0 or math.isnan(atr):
            continue
        stop = entry - combo.atr * atr
        target = entry + combo.atr * combo.reward_risk * atr
        plan = calculate_plan(entry, stop, target)
        if plan is None:
            continue
        future = frame.loc[index + 1 : index + horizon_bars]
        for _, candle in future.iterrows():
            if float(candle.low) <= stop:
                results.append(-abs(plan.net_loss_sl)); break
            if float(candle.high) >= target:
                results.append(plan.net_profit_tp1); break
    return results


def metric_dict(results: list[float]) -> dict[str, float | int]:
    if not results:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "expectancy": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "max_dd": 0.0, "sharpe": 0.0, "sortino": 0.0, "net_profit": 0.0}
    wins = [x for x in results if x > 0]; losses = [x for x in results if x < 0]
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    equity = TRADE_AMOUNT_EUR; peak = equity; max_dd = 0.0
    for result in results:
        equity += result; peak = max(peak, equity); max_dd = max(max_dd, peak - equity)
    exp = sum(results) / len(results)
    std = pd.Series(results).std() or 0.0
    downside = pd.Series([min(0, x) for x in results]).std() or 0.0
    return {"trades": len(results), "win_rate": len(wins) / len(results) * 100, "profit_factor": (gross_profit / gross_loss if gross_loss else gross_profit), "expectancy": exp, "avg_win": mean(wins) if wins else 0.0, "avg_loss": mean(losses) if losses else 0.0, "max_dd": max_dd, "sharpe": (exp / std * math.sqrt(len(results)) if std else 0.0), "sortino": (exp / downside * math.sqrt(len(results)) if downside else 0.0), "net_profit": sum(results)}


def validation_reason(total: dict, validation: dict) -> tuple[str, str]:
    reasons = []
    if total["trades"] < RESEARCH_MIN_TRADES: reasons.append(f"Numero trade inferiore al requisito {RESEARCH_MIN_TRADES}.")
    if total["profit_factor"] < 1.25: reasons.append("Profit Factor inferiore al requisito 1.25.")
    if total["expectancy"] <= 0: reasons.append("Expectancy non positiva.")
    if validation["profit_factor"] < 1.10: reasons.append("Profit Factor validation inferiore a 1.10.")
    if validation["expectancy"] <= 0: reasons.append("Expectancy validation non positiva.")
    return ("VALIDATA" if not reasons else "NON VALIDATA", " ".join(reasons) or "Criteri minimi soddisfatti.")


def store_result(combo: Combination, total: dict, train: dict, validation: dict, status: str, reason: str) -> None:
    with sqlite3.connect(RESEARCH_DB_PATH) as conn:
        conn.execute(
            """INSERT INTO strategy_results(strategy,pair,timeframe,market_regime,rsi_range,adx,atr,relative_volume,reward_risk,tp_method,sl_method,number_of_trades,win_rate,profit_factor,expectancy_eur,average_win,average_loss,max_drawdown,sharpe,sortino,net_profit,training_result,validation_result,validation_profit_factor,validation_expectancy,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (combo.strategy, combo.pair, combo.timeframe, combo.market_regime, f"{combo.rsi_range[0]}-{combo.rsi_range[1]}", combo.adx, combo.atr, combo.relative_volume, combo.reward_risk, TP_METHOD, SL_METHOD, total["trades"], total["win_rate"], total["profit_factor"], total["expectancy"], total["avg_win"], total["avg_loss"], total["max_dd"], total["sharpe"], total["sortino"], total["net_profit"], f"PF {train['profit_factor']:.2f} EXP {train['expectancy']:.4f}", f"{status}: {reason}", validation["profit_factor"], validation["expectancy"], datetime.now(timezone.utc).isoformat()),
        )


def _load_frame(pair: str, timeframe: str) -> pd.DataFrame:
    frame = add_indicators(sync_ohlc_cache(pair, timeframe))
    return closed_candles(frame, timeframe).reset_index(drop=True)


def run_research() -> str:
    init_ohlc_cache(); init_research_db()
    total = total_combinations(); start_index = _latest_progress(); started_at = datetime.now(timezone.utc).isoformat(); started = time.time(); batch_id = start_index // RESEARCH_BATCH_SIZE + 1
    logger.info("Quant Research Engine total_combinations=%s resume_from=%s batch_size=%s", total, start_index, RESEARCH_BATCH_SIZE)
    cache: dict[tuple[str, str], pd.DataFrame] = {}
    processed = start_index; last_combo = None
    for combo in itertools.islice(generate_combinations(), start_index, None):
        if time.time() - started > MAX_RESEARCH_RUNTIME_MINUTES * 60:
            _save_progress(batch_id, total, processed, last_combo, "PAUSED_RUNTIME_LIMIT", started_at); break
        last_combo = combo
        key = (combo.pair, combo.timeframe)
        if key not in cache:
            try: cache[key] = _load_frame(*key)
            except Exception as exc:
                logger.exception("OHLC load failed pair=%s timeframe=%s: %s", combo.pair, combo.timeframe, exc); cache[key] = pd.DataFrame()
        frame = cache[key]
        if len(frame) >= 220:
            results = evaluate(frame, combo)
            split = int(len(results) * 0.7); train = metric_dict(results[:split]); validation = metric_dict(results[split:]); total_metrics = metric_dict(results)
            status, reason = validation_reason(total_metrics, validation)
            store_result(combo, total_metrics, train, validation, status, reason)
        processed += 1
        if processed % RESEARCH_BATCH_SIZE == 0:
            batch_id = processed // RESEARCH_BATCH_SIZE
            _save_progress(batch_id, total, processed, combo, "RUNNING", started_at)
            logger.info("Research batch=%s processed=%s/%s", batch_id, processed, total)
    if processed >= total: _save_progress(batch_id, total, processed, last_combo, "COMPLETED", started_at)
    return export_reports(processed, total)


def _query(sql: str) -> list[sqlite3.Row]:
    with sqlite3.connect(RESEARCH_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql).fetchall()


def _export_csv(filename: str, sql: str) -> str:
    rows = _query(sql); path = Path(EXPORT_DIR) / filename
    if rows:
        with path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(dict(r) for r in rows)
    else: path.write_text("")
    return str(path)


def export_reports(processed: int, total: int) -> str:
    exports = [
        _export_csv("top100_profit_factor.csv", "SELECT * FROM strategy_results ORDER BY profit_factor DESC LIMIT 100"),
        _export_csv("top100_expectancy.csv", "SELECT * FROM strategy_results ORDER BY expectancy_eur DESC LIMIT 100"),
        _export_csv("top100_winrate.csv", "SELECT * FROM strategy_results ORDER BY win_rate DESC LIMIT 100"),
        _export_csv("top100_sharpe.csv", "SELECT * FROM strategy_results ORDER BY sharpe DESC LIMIT 100"),
        _export_csv("top100_netprofit.csv", "SELECT * FROM strategy_results ORDER BY net_profit DESC LIMIT 100"),
        _export_csv("top100_lowest_drawdown.csv", "SELECT * FROM strategy_results ORDER BY max_drawdown ASC, profit_factor DESC LIMIT 100"),
    ]
    top20 = _query("SELECT * FROM strategy_results ORDER BY profit_factor DESC, expectancy_eur DESC LIMIT 20")
    top_pair = _query("SELECT pair, AVG(profit_factor) avg_pf, AVG(expectancy_eur) avg_exp FROM strategy_results GROUP BY pair ORDER BY avg_pf DESC LIMIT 10")
    top_tf = _query("SELECT timeframe, AVG(profit_factor) avg_pf FROM strategy_results GROUP BY timeframe ORDER BY avg_pf DESC")
    top_strategy = _query("SELECT strategy, AVG(profit_factor) avg_pf, AVG(expectancy_eur) avg_exp FROM strategy_results GROUP BY strategy ORDER BY avg_pf DESC")
    top_regime = _query("SELECT market_regime, AVG(profit_factor) avg_pf FROM strategy_results GROUP BY market_regime ORDER BY avg_pf DESC")
    worst_regime = _query("SELECT market_regime, AVG(profit_factor) avg_pf FROM strategy_results GROUP BY market_regime ORDER BY avg_pf ASC")
    lines = ["================================================", "TOP 20 STRATEGIE", "================================================"]
    for r in top20:
        lines += [f"{r['strategy']} | {r['pair']} | {r['timeframe']} | {r['number_of_trades']} trade | Win Rate {r['win_rate']:.2f}% | PF {r['profit_factor']:.2f} | Expectancy €{r['expectancy_eur']:.4f} | Sharpe {r['sharpe']:.2f} | Max DD €{r['max_drawdown']:.2f}", f"Training: {r['training_result']} | Validation: {r['validation_result']}", ""]
    def section(title, rows, cols):
        lines.extend(["================================================", title, "================================================"])
        for row in rows: lines.append(" | ".join(f"{c}: {row[c]:.4f}" if isinstance(row[c], float) else f"{c}: {row[c]}" for c in cols))
    section("TOP PAIR", top_pair, ["pair", "avg_pf", "avg_exp"]); section("TOP TIMEFRAME", top_tf, ["timeframe", "avg_pf"]); section("TOP STRATEGY", top_strategy, ["strategy", "avg_pf", "avg_exp"]); section("TOP REGIMI", top_regime, ["market_regime", "avg_pf"]); section("PEGGIORI REGIMI", worst_regime, ["market_regime", "avg_pf"])
    lines.extend(["================================================", "TOP ORARI", "================================================", "Analisi oraria pronta per estensione: i trade vengono salvati come risultati aggregati strategia/parametri; aggiungere persistenza per ora di entry per ranking intraday.", "================================================", "PEGGIORI ORARI", "================================================", "Analisi oraria pronta per estensione: nessun filtro orario viene applicato automaticamente al bot live."])
    if top_strategy: lines.append(f"Analisi automatica: le strategie più promettenti sono {top_strategy[0]['strategy']} con PF medio {top_strategy[0]['avg_pf']:.2f}.")
    if top_pair: lines.append(f"Pair da approfondire: {top_pair[0]['pair']}.")
    if top_tf: lines.append(f"Timeframe da approfondire: {top_tf[0]['timeframe']}.")
    lines.append("Suggerimento: approfondire le combinazioni nelle CSV top100 prima di modificare il bot live.")
    summary_path = Path(EXPORT_DIR) / "research_summary.txt"; summary_path.write_text("\n".join(lines))
    exports.append(str(summary_path))
    return f"🔬 Quant Research Engine\nCombinazioni processate: {processed}/{total}\nFile esportati:\n" + "\n".join(exports) + "\n\n" + "\n".join(lines[:80])
