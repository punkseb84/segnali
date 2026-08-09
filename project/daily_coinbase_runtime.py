"""Daily Coinbase intraday PAPER scanner.

Every day at 13:00 Europe/Rome:
- builds a 40-crypto Coinbase USD universe;
- analyzes 15m, 30m and 1h closed candles;
- ranks long-only spot setups and sends the top five to Telegram;
- tracks the five paper trades until TP, SL or the next local midnight.

At the date rollover every still-open trade is closed at the current Coinbase bid,
a net daily recap is sent, and the next day starts with a clean batch.

No real exchange orders are ever submitted.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import json
import math
import os
import time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from project.config.settings import load_settings
from project.database.postgres import PostgresClient, PostgresConfig
from project.notification_engine.service import NotificationEngine
from project.shared.events import EventBus
from project.shared.logging import get_module_logger

ROME = ZoneInfo("Europe/Rome")
COINBASE_EXCHANGE = "https://api.exchange.coinbase.com"
COINBASE_ADVANCED_PUBLIC = "https://api.coinbase.com/api/v3/brokerage/market"
ANALYSIS_HOUR = 13
SIGNAL_COUNT = 5
UNIVERSE_SIZE = 40
DAILY_BUDGET_EUR = float(os.getenv("DAILY_PAPER_BUDGET_EUR", "100"))
TRADE_NOTIONAL_EUR = DAILY_BUDGET_EUR / SIGNAL_COUNT

# User-requested Coinbase VIP1 model. Default: Coinbase International Spot
# Public Tier 1 taker pricing (2 bps per side). Keep configurable because
# Coinbase fees are account/product dependent.
COINBASE_TAKER_FEE = float(os.getenv("COINBASE_VIP1_TAKER_FEE_RATE", "0.0002"))
MONITOR_SECONDS = max(20, int(os.getenv("DAILY_SIGNAL_MONITOR_SECONDS", "60")))
HTTP_TIMEOUT = 15

EXCLUDED_BASES = {
    "USD", "USDC", "USDT", "DAI", "EURC", "PYUSD",
    "WBTC", "CBETH", "WSTETH", "PAX", "GUSD",
}
PREFERRED_BASES = [
    "BTC","ETH","SOL","XRP","DOGE","ADA","AVAX","LINK","LTC","BCH",
    "DOT","UNI","AAVE","XLM","ATOM","ETC","FIL","NEAR","ICP","INJ",
    "OP","ARB","SUI","APT","SEI","HBAR","ALGO","PEPE","SHIB","BONK",
    "WIF","CRV","SKY","MKR","COMP","SNX","GRT","LDO","RENDER","FET",
    "IMX","STX","MANA","SAND","AXS","APE","FLOW","XTZ","EOS","KSM",
    "ZEC","DASH","ENS","JASMY","QNT","RPL","MINA","ROSE","BLUR","1INCH",
    "ANKR","BAT","CHZ","COTI","DNT","ENJ","MASK","OCEAN","OMG","RLC",
    "UMA","YFI","ZRX","CELO","CVC","KNC","LRC","NMR","BAND",
]

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS daily_coinbase;
CREATE TABLE IF NOT EXISTS daily_coinbase.batches (
    trade_date DATE PRIMARY KEY,
    analysis_at TIMESTAMPTZ NOT NULL,
    universe JSONB NOT NULL,
    fee_rate DOUBLE PRECISION NOT NULL,
    paper_budget_eur DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    recap_sent BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS daily_coinbase.trades (
    id BIGSERIAL PRIMARY KEY,
    trade_date DATE NOT NULL REFERENCES daily_coinbase.batches(trade_date) ON DELETE CASCADE,
    rank INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    setup TEXT NOT NULL,
    entry DOUBLE PRECISION NOT NULL,
    stop_loss DOUBLE PRECISION NOT NULL,
    take_profit DOUBLE PRECISION NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    reason TEXT NOT NULL,
    net_tp_pct DOUBLE PRECISION NOT NULL,
    net_sl_pct DOUBLE PRECISION NOT NULL,
    net_rr DOUBLE PRECISION NOT NULL,
    notional_eur DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    exit_price DOUBLE PRECISION,
    net_return_pct DOUBLE PRECISION,
    net_pnl_eur DOUBLE PRECISION,
    close_reason TEXT,
    UNIQUE(trade_date, rank),
    UNIQUE(trade_date, product_id)
);
"""


@dataclass
class Candidate:
    product_id: str
    score: float
    setup: str
    entry: float
    stop: float
    target: float
    net_tp_pct: float
    net_sl_pct: float
    net_rr: float
    reason: str


class DailyCoinbaseRuntime:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.logger = get_module_logger("daily-coinbase-runtime")
        self.postgres = PostgresClient(PostgresConfig(self.settings.database_url))
        self.postgres.test_connection()
        self._ensure_schema()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "daily-coinbase-paper-scanner/1.0",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        })
        self.notifier = NotificationEngine(
            EventBus(), enabled=True,
            max_message_length=self.settings.telegram_max_message_length,
            max_retries=self.settings.telegram_max_retries,
            postgres=self.postgres,
        )

    def _ensure_schema(self) -> None:
        for statement in [part.strip() for part in SCHEMA_SQL.split(";") if part.strip()]:
            self.postgres.execute(statement)

    def send(self, message: str) -> None:
        if not self.notifier.send_telegram(message):
            self.logger.warning("Telegram delivery returned no result")

    @staticmethod
    def _now_utc() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _today_rome():
        return datetime.now(ROME).date()

    def _get_json(self, url: str, *, params: dict[str, Any] | None = None, retries: int = 3) -> Any:
        last: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=HTTP_TIMEOUT)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last = exc
                if attempt < retries:
                    time.sleep(0.6 * attempt)
        raise RuntimeError(f"HTTP_GET_FAILED url={url} error={last}")

    def _available_usd_products(self) -> tuple[list[str], dict[str, float]]:
        products: list[str] = []
        quote_volume: dict[str, float] = {}
        try:
            payload = self._get_json(
                f"{COINBASE_ADVANCED_PUBLIC}/products",
                params={"product_type": "SPOT"},
            )
            rows = payload.get("products") or []
            for row in rows:
                pid = str(row.get("product_id") or "").upper()
                if not pid.endswith("-USD"):
                    continue
                base = pid[:-4]
                if base in EXCLUDED_BASES:
                    continue
                status = str(row.get("status") or "").upper()
                if bool(row.get("trading_disabled")) or status in {"OFFLINE", "DELISTED"}:
                    continue
                qv_raw = row.get("approximate_quote_24h_volume")
                if qv_raw in (None, ""):
                    try:
                        qv_raw = float(row.get("volume_24h") or 0.0) * float(row.get("price") or 0.0)
                    except (TypeError, ValueError):
                        qv_raw = 0.0
                try:
                    qv = float(qv_raw or 0.0)
                except (TypeError, ValueError):
                    qv = 0.0
                products.append(pid)
                quote_volume[pid] = max(0.0, qv)
            products = sorted(set(products), key=lambda p: quote_volume.get(p, 0.0), reverse=True)
        except Exception as exc:
            self.logger.warning("Advanced public product discovery failed: %s", exc)

        if len(products) < UNIVERSE_SIZE:
            payload = self._get_json(f"{COINBASE_EXCHANGE}/products")
            online = {
                str(row.get("id") or "").upper()
                for row in payload
                if str(row.get("quote_currency") or "").upper() == "USD"
                and str(row.get("status") or "").lower() == "online"
                and str(row.get("base_currency") or "").upper() not in EXCLUDED_BASES
            }
            ordered = [f"{base}-USD" for base in PREFERRED_BASES if f"{base}-USD" in online]
            extras = sorted(online.difference(ordered))
            products = list(dict.fromkeys(products + ordered + extras))

        if len(products) < UNIVERSE_SIZE:
            raise RuntimeError(f"COINBASE_USD_UNIVERSE_TOO_SMALL available={len(products)}")
        selected = products[:UNIVERSE_SIZE]
        liquidity_rank = {pid: float(i + 1) for i, pid in enumerate(selected)}
        return selected, liquidity_rank

    def _candles(self, product_id: str, granularity: int) -> pd.DataFrame:
        payload = self._get_json(
            f"{COINBASE_EXCHANGE}/products/{product_id}/candles",
            params={"granularity": granularity},
        )
        if not isinstance(payload, list):
            raise RuntimeError(f"INVALID_CANDLES product={product_id} granularity={granularity}")
        rows = []
        for item in payload:
            if not isinstance(item, list) or len(item) < 6:
                continue
            rows.append({
                "timestamp": pd.Timestamp(int(item[0]), unit="s", tz="UTC"),
                "low": float(item[1]), "high": float(item[2]),
                "open": float(item[3]), "close": float(item[4]),
                "volume": float(item[5]),
            })
        x = pd.DataFrame(rows)
        if x.empty:
            raise RuntimeError(f"EMPTY_CANDLES product={product_id} granularity={granularity}")
        x = x.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
        now = pd.Timestamp.now(tz="UTC")
        x = x[(x["timestamp"] + pd.Timedelta(seconds=granularity)) <= now]
        return x.reset_index(drop=True)

    def _market_snapshot(self, product_id: str) -> dict[str, float]:
        payload = self._get_json(f"{COINBASE_EXCHANGE}/products/{product_id}/ticker")
        price = float(payload.get("price") or 0.0)
        bid = float(payload.get("bid") or price)
        ask = float(payload.get("ask") or price)
        return {"price": price, "bid": bid or price, "ask": ask or price}

    @staticmethod
    def _resample_30m(x: pd.DataFrame) -> pd.DataFrame:
        y = x.set_index("timestamp").resample(
            "30min", origin="epoch", label="left", closed="left"
        ).agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna().reset_index()
        now = pd.Timestamp.now(tz="UTC")
        return y[(y["timestamp"] + pd.Timedelta(minutes=30)) <= now].reset_index(drop=True)

    @staticmethod
    def _ema(s: pd.Series, span: int) -> pd.Series:
        return s.astype(float).ewm(span=span, adjust=False).mean()

    @staticmethod
    def _rsi(s: pd.Series, period: int = 14) -> pd.Series:
        delta = s.astype(float).diff()
        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)
        avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0.0, math.nan)
        return 100.0 - 100.0 / (1.0 + rs)

    @classmethod
    def _macd_hist(cls, s: pd.Series) -> pd.Series:
        fast = cls._ema(s, 12)
        slow = cls._ema(s, 26)
        macd = fast - slow
        signal = macd.ewm(span=9, adjust=False).mean()
        return macd - signal

    @staticmethod
    def _atr(x: pd.DataFrame, period: int = 14) -> pd.Series:
        prev_close = x["close"].astype(float).shift(1)
        tr = pd.concat([
            x["high"].astype(float) - x["low"].astype(float),
            (x["high"].astype(float) - prev_close).abs(),
            (x["low"].astype(float) - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    @classmethod
    def _features(cls, x: pd.DataFrame) -> dict[str, Any]:
        close = x["close"].astype(float)
        ema20, ema50, ema200 = cls._ema(close, 20), cls._ema(close, 50), cls._ema(close, 200)
        rsi = cls._rsi(close)
        macd = cls._macd_hist(close)
        atr = cls._atr(x)
        vol_ma = x["volume"].astype(float).rolling(20).mean()
        rvol = x["volume"].astype(float) / vol_ma.replace(0.0, math.nan)
        prev20 = x["high"].astype(float).rolling(20).max().shift(1)
        bb_mid = close.rolling(20).mean()
        bb_std = close.rolling(20).std(ddof=0)
        z = (close - bb_mid) / bb_std.replace(0.0, math.nan)
        rv = float(rvol.iloc[-1])
        zv = float(z.iloc[-1])
        return {
            "close": float(close.iloc[-1]), "open": float(x["open"].iloc[-1]),
            "ema20": float(ema20.iloc[-1]), "ema50": float(ema50.iloc[-1]),
            "ema200": float(ema200.iloc[-1]), "ema20_prev4": float(ema20.iloc[-5]),
            "rsi": float(rsi.iloc[-1]), "rsi_prev": float(rsi.iloc[-2]),
            "macd": float(macd.iloc[-1]), "macd_prev": float(macd.iloc[-2]),
            "atr": float(atr.iloc[-1]), "rvol": rv if math.isfinite(rv) else 0.0,
            "prev20_high": float(prev20.iloc[-1]), "z20": zv if math.isfinite(zv) else 0.0,
            "ret4": float(close.iloc[-1] / close.iloc[-5] - 1.0) if len(close) >= 5 else 0.0,
            "ret24": float(close.iloc[-1] / close.iloc[-25] - 1.0) if len(close) >= 25 else 0.0,
        }

    @staticmethod
    def _net_return(entry: float, exit_price: float) -> float:
        if entry <= 0 or exit_price <= 0:
            return 0.0
        return (exit_price * (1.0 - COINBASE_TAKER_FEE)) / (
            entry * (1.0 + COINBASE_TAKER_FEE)
        ) - 1.0

    @staticmethod
    def _target_price_for_net(entry: float, target_net: float) -> float:
        return entry * (1.0 + COINBASE_TAKER_FEE) * (1.0 + target_net) / (
            1.0 - COINBASE_TAKER_FEE
        )

    def _score_candidate(
        self, product_id: str, x15: pd.DataFrame, x1h: pd.DataFrame,
        btc1h: pd.DataFrame, liquidity_rank: float,
    ) -> Candidate | None:
        if len(x15) < 220 or len(x1h) < 220 or len(btc1h) < 30:
            return None
        x30 = self._resample_30m(x15)
        if len(x30) < 80:
            return None

        f15, f30, f1 = self._features(x15), self._features(x30), self._features(x1h)
        btc = self._features(btc1h)
        if not all(math.isfinite(float(v)) for v in [
            f15["close"], f15["atr"], f30["rsi"], f1["ema200"], f1["rsi"]
        ]):
            return None

        trend = 0.0
        trend_reasons: list[str] = []
        if f1["close"] > f1["ema20"]: trend += 8
        if f1["ema20"] > f1["ema50"]: trend += 8
        if f1["ema50"] > f1["ema200"]:
            trend += 10
            trend_reasons.append("struttura 1h sopra EMA20/50/200")
        if f1["ema20"] > f1["ema20_prev4"]: trend += 5
        if 50 <= f1["rsi"] <= 70:
            trend += 6
            trend_reasons.append(f"RSI 1h {f1['rsi']:.0f} in zona momentum")
        if f1["macd"] > 0 and f1["macd"] > f1["macd_prev"]: trend += 6

        if f30["close"] > f30["ema20"]: trend += 5
        if 50 <= f30["rsi"] <= 68: trend += 6
        if f30["macd"] > 0:
            trend += 5
            trend_reasons.append("momentum 30m positivo")
        if f30["ret4"] > 0: trend += 3

        breakout = f15["close"] > f15["prev20_high"] and f15["close"] > f15["open"]
        pullback = (
            f1["ema20"] > f1["ema50"] and f15["close"] > f15["ema50"]
            and abs(f15["close"] - f15["ema20"]) / f15["close"] <= 0.007
            and f15["close"] > f15["open"]
        )
        if breakout:
            trend += 10
            trend_reasons.append("breakout 15m dei massimi recenti")
        elif pullback:
            trend += 8
            trend_reasons.append("pullback 15m su EMA20 con reazione rialzista")
        elif f15["close"] > f15["ema20"]:
            trend += 3
        if f15["rvol"] >= 1.5:
            trend += 7
            trend_reasons.append(f"volume 15m {f15['rvol']:.1f}× la media")
        elif f15["rvol"] >= 1.1:
            trend += 4
        if 48 <= f15["rsi"] <= 68: trend += 5
        if f15["macd"] > 0 and f15["macd"] > f15["macd_prev"]: trend += 5

        rs4 = f1["ret4"] - btc["ret4"]
        rs24 = f1["ret24"] - btc["ret24"]
        if rs4 >= 0.005:
            trend += 8
            trend_reasons.append(f"forza relativa vs BTC +{100*rs4:.1f}% su 4h")
        elif rs4 > 0:
            trend += 4
        if rs24 >= 0.01: trend += 7
        elif rs24 > 0: trend += 3

        atr_pct = f15["atr"] / f15["close"]
        if 0.003 <= atr_pct <= 0.025: trend += 5
        extension = f1["close"] / max(f1["ema20"], 1e-12) - 1.0
        if 0 <= extension <= 0.035: trend += 4
        elif extension > 0.06: trend -= 8

        revert = 0.0
        revert_reasons: list[str] = []
        if 28 <= f1["rsi"] <= 43:
            revert += 18
            revert_reasons.append(f"RSI 1h depresso ({f1['rsi']:.0f})")
        if f1["z20"] <= -1.5:
            revert += 15
            revert_reasons.append("deviazione 1h estrema sotto la media")
        if f15["rsi_prev"] < 38 and f15["rsi"] > f15["rsi_prev"]:
            revert += 14
            revert_reasons.append("RSI 15m in recupero da ipervenduto")
        if f15["close"] > f15["open"] and f15["macd"] > f15["macd_prev"]: revert += 12
        if f15["rvol"] >= 1.5: revert += 8
        if rs4 <= -0.015 and f15["ret4"] > -0.005:
            revert += 12
            revert_reasons.append("sottoperformance vs BTC con stabilizzazione 15m")
        if f30["rsi"] > f15["rsi"]: revert += 4
        if f1["close"] > f1["ema200"]: revert += 5

        liq_bonus = 5 if liquidity_rank <= 10 else 3 if liquidity_rank <= 20 else 1
        trend += liq_bonus
        revert += liq_bonus
        if trend >= revert:
            score, setup, reasons = trend, "TREND", trend_reasons
        else:
            score, setup, reasons = revert, "REVERSION", revert_reasons

        snapshot = self._market_snapshot(product_id)
        entry = float(snapshot["ask"])
        if entry <= 0:
            return None
        swing_low = float(x15["low"].astype(float).iloc[-12:].min())
        structural_pct = max(0.0, (entry - swing_low) / entry + 0.0015)
        atr_stop_pct = 1.35 * f15["atr"] / entry
        stop_pct = min(0.028, max(0.0065, structural_pct, atr_stop_pct))
        stop = entry * (1.0 - stop_pct)
        net_sl = self._net_return(entry, stop)
        risk = abs(net_sl)
        if risk <= 0:
            return None

        atr1h_pct = float(self._atr(x1h).iloc[-1]) / entry
        expected_move = min(0.045, max(0.012, 1.35 * atr1h_pct, 2.3 * atr_pct))
        target_net = min(0.05, max(1.65 * risk, expected_move))
        target = self._target_price_for_net(entry, target_net)
        net_tp = self._net_return(entry, target)
        net_rr = net_tp / risk if risk else 0.0
        if net_rr < 1.55: score -= 8
        if stop_pct >= 0.027: score -= 5

        if not reasons:
            reasons = ["migliore combinazione relativa fra i 40 asset analizzati"]
        reason = "; ".join(reasons[:4])
        reason += (
            f". Il setup {setup.lower()} ha score {score:.0f}/100, "
            f"R/R netto {net_rr:.2f} e volatilità 15m {100*atr_pct:.2f}%."
        )
        return Candidate(
            product_id, score, setup, entry, stop, target,
            100.0 * net_tp, 100.0 * net_sl, net_rr, reason,
        )

    def _load_product_frames(self, product_id: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        return self._candles(product_id, 900), self._candles(product_id, 3600)

    def analyze_and_open_daily_batch(self, local_now: datetime) -> None:
        trade_date = local_now.date()
        if self.postgres.fetch_all(
            "SELECT 1 FROM daily_coinbase.batches WHERE trade_date=%s LIMIT 1", (trade_date,)
        ):
            return

        universe, ranks = self._available_usd_products()
        self.logger.info("DAILY_13_SCAN universe=%s", universe)
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

        candidates: list[Candidate] = []
        for pid in universe:
            pair_frames = frames.get(pid)
            if pair_frames is None:
                continue
            try:
                candidate = self._score_candidate(
                    pid, pair_frames[0], pair_frames[1], btc1h, ranks.get(pid, 40.0)
                )
                if candidate is not None:
                    candidates.append(candidate)
            except Exception as exc:
                self.logger.warning("CANDIDATE_SCORE_FAILED product=%s error=%s", pid, exc)

        candidates.sort(key=lambda c: (c.score, c.net_rr), reverse=True)
        selected = candidates[:SIGNAL_COUNT]
        if len(selected) < SIGNAL_COUNT:
            raise RuntimeError(f"INSUFFICIENT_VALID_SETUPS valid={len(selected)} required={SIGNAL_COUNT}")

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
                       trade_date, rank, product_id, setup, entry, stop_loss, take_profit,
                       score, reason, net_tp_pct, net_sl_pct, net_rr, notional_eur, status, opened_at
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'OPEN',%s)
                   ON CONFLICT (trade_date, rank) DO NOTHING""",
                (trade_date, rank, c.product_id, c.setup, c.entry, c.stop, c.target,
                 c.score, c.reason, c.net_tp_pct, c.net_sl_pct, c.net_rr,
                 TRADE_NOTIONAL_EUR, analysis_at),
            )

        self.send(
            "📊 <b>SCANNER CRYPTO · 13:00</b>\n"
            f"Analizzate <b>{len(universe)} crypto</b> su Coinbase · timeframe 15m / 30m / 1h\n"
            f"Top <b>{SIGNAL_COUNT}</b> setup LONG PAPER · €{TRADE_NOTIONAL_EUR:.2f} per operazione\n"
            f"Fee modello Coinbase VIP1 taker: <b>{100*COINBASE_TAKER_FEE:.3f}% per lato</b>\n"
            "TP/SL e R/R sono già calcolati al netto delle commissioni.\n"
            "⚠️ Selezione probabilistica, non garanzia di profitto."
        )
        for rank, c in enumerate(selected, 1):
            self.send(self._format_signal(rank, c))

    @staticmethod
    def _price_digits(value: float) -> int:
        if value >= 1000: return 2
        if value >= 100: return 3
        if value >= 1: return 4
        if value >= 0.01: return 6
        return 8

    def _format_signal(self, rank: int, c: Candidate) -> str:
        d = self._price_digits(c.entry)
        return (
            f"🟢 <b>SEGNALE #{rank} · {escape(c.product_id)}</b>\n"
            f"Direzione: <b>LONG</b> · Setup: <b>{c.setup}</b>\n"
            f"Entry: <b>{c.entry:.{d}f}</b>\n"
            f"Stop Loss: <b>{c.stop:.{d}f}</b> · netto {c.net_sl_pct:+.2f}%\n"
            f"Take Profit: <b>{c.target:.{d}f}</b> · netto {c.net_tp_pct:+.2f}%\n"
            f"R/R netto: <b>{c.net_rr:.2f}</b> · Score: <b>{c.score:.0f}/100</b>\n\n"
            f"📝 <b>Perché è potenzialmente profittevole</b>\n{escape(c.reason)}"
        )

    def _open_trades(self) -> list[tuple[Any, ...]]:
        return self.postgres.fetch_all(
            """SELECT id, trade_date, rank, product_id, entry, stop_loss, take_profit,
                      notional_eur, setup
               FROM daily_coinbase.trades WHERE status='OPEN'
               ORDER BY trade_date, rank"""
        )

    def _close_trade(
        self, trade_id: int, entry: float, exit_price: float,
        reason: str, notional: float, closed_at: datetime,
    ) -> tuple[float, float]:
        net_r = self._net_return(entry, exit_price)
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
            trade_id, trade_date, rank, pid, entry, stop, target, notional, setup = row
            if trade_date < self._today_rome():
                continue
            try:
                bid = float(self._market_snapshot(str(pid))["bid"])
            except Exception as exc:
                self.logger.warning("MONITOR_PRICE_FAILED product=%s error=%s", pid, exc)
                continue
            close_reason: str | None = None
            if bid <= float(stop): close_reason = "SL"
            elif bid >= float(target): close_reason = "TP"
            if close_reason is None:
                continue
            net_pct, pnl = self._close_trade(
                int(trade_id), float(entry), bid, close_reason, float(notional), self._now_utc()
            )
            icon = "✅" if close_reason == "TP" else "🔴"
            d = self._price_digits(bid)
            self.send(
                f"{icon} <b>{close_reason} · {escape(str(pid))}</b>\n"
                f"Segnale #{rank} · {escape(str(setup))}\n"
                f"Uscita: <b>{bid:.{d}f}</b>\n"
                f"Risultato netto: <b>{net_pct:+.2f}% · €{pnl:+.2f}</b>"
            )

    def _fallback_exit(self, product_id: str) -> float:
        try:
            return float(self._market_snapshot(product_id)["bid"])
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
                """SELECT id, rank, product_id, entry, notional_eur
                   FROM daily_coinbase.trades
                   WHERE trade_date=%s AND status='OPEN' ORDER BY rank""",
                (trade_date,),
            )
            for trade_id, rank, pid, entry, notional in rows:
                try:
                    exit_price = self._fallback_exit(str(pid))
                    self._close_trade(
                        int(trade_id), float(entry), exit_price, "MIDNIGHT",
                        float(notional), self._now_utc(),
                    )
                except Exception as exc:
                    self.logger.exception(
                        "MIDNIGHT_CLOSE_FAILED date=%s product=%s error=%s", trade_date, pid, exc
                    )
            self._send_recap(trade_date)
            self.postgres.execute(
                """UPDATE daily_coinbase.batches
                   SET status='CLOSED', recap_sent=TRUE, closed_at=NOW()
                   WHERE trade_date=%s""", (trade_date,)
            )

    def _send_recap(self, trade_date) -> None:
        rows = self.postgres.fetch_all(
            """SELECT rank, product_id, status, entry, exit_price,
                      net_return_pct, net_pnl_eur, close_reason
               FROM daily_coinbase.trades WHERE trade_date=%s ORDER BY rank""",
            (trade_date,),
        )
        total = sum(float(r[6] or 0.0) for r in rows)
        wins = sum(1 for r in rows if float(r[6] or 0.0) > 0)
        losses = sum(1 for r in rows if float(r[6] or 0.0) < 0)
        flat = len(rows) - wins - losses
        lines = [
            f"🌙 <b>RESOCONTO GIORNALIERO · {trade_date:%d/%m/%Y}</b>",
            f"Operazioni: <b>{len(rows)}</b> · positive {wins} · negative {losses} · flat {flat}", "",
        ]
        for rank, pid, status, entry, exit_price, net_pct, pnl, close_reason in rows:
            value = float(pnl or 0.0)
            icon = "🟢" if value > 0 else "🔴" if value < 0 else "⚪️"
            lines.append(
                f"{icon} #{rank} <b>{escape(str(pid))}</b> · {escape(str(close_reason or status))} · "
                f"{float(net_pct or 0.0):+.2f}% · €{value:+.2f}"
            )
        lines += [
            "", f"Risultato netto giornata: <b>€{total:+.2f}</b>",
            f"Budget PAPER di riferimento: €{DAILY_BUDGET_EUR:.2f} → <b>€{DAILY_BUDGET_EUR + total:.2f}</b>",
            "🔄 <b>RESET COMPLETATO</b>: nessuna operazione viene trascinata al giorno successivo.",
        ]
        self.send("\n".join(lines))

    def _send_startup(self) -> None:
        self.send(
            "🤖 <b>DAILY COINBASE SIGNALS · ATTIVO</b>\n"
            "Ogni giorno alle <b>13:00 Europe/Rome</b>: 40 crypto, TF 15m/30m/1h, top 5 segnali LONG PAPER.\n"
            "Alle <b>00:00</b>: chiusura forzata delle operazioni ancora aperte, resoconto netto e reset.\n"
            f"Fee VIP1 modellata: <b>{100*COINBASE_TAKER_FEE:.3f}% taker per lato</b>."
        )

    def run_forever(self) -> None:
        self.logger.info(
            "DAILY_COINBASE_RUNTIME_STARTED analysis=13:00 timezone=Europe/Rome universe=%s signals=%s "
            "paper_budget=%.2f trade_notional=%.2f fee_per_side=%.5f",
            UNIVERSE_SIZE, SIGNAL_COUNT, DAILY_BUDGET_EUR, TRADE_NOTIONAL_EUR, COINBASE_TAKER_FEE,
        )
        self._send_startup()
        last_monitor = 0.0
        while True:
            try:
                local_now = datetime.now(ROME)
                self.finalize_previous_days(local_now)
                if local_now.hour >= ANALYSIS_HOUR and (local_now.hour < 23 or local_now.minute < 30):
                    self.analyze_and_open_daily_batch(local_now)
                now_mono = time.monotonic()
                if now_mono - last_monitor >= MONITOR_SECONDS:
                    self.monitor_open_trades()
                    last_monitor = now_mono
            except Exception as exc:
                self.logger.exception("DAILY_RUNTIME_LOOP_ERROR error=%s", exc)
                try:
                    self.send(
                        "⚠️ <b>DAILY COINBASE SIGNALS</b>\n"
                        f"Errore operativo: <code>{escape(str(exc)[:700])}</code>\n"
                        "Il runtime ritenterà automaticamente."
                    )
                except Exception:
                    pass
            time.sleep(20)


def main() -> None:
    DailyCoinbaseRuntime().run_forever()


if __name__ == "__main__":
    main()
