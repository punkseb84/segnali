"""Resilient frozen 12-month Binance Spot audit for CAND000006.

V2 keeps the exact same strategy, dates, costs and gates as V1.  The only
change is data acquisition: official Binance Vision monthly/daily ZIP archives
are used first, with the Spot market-data REST API as fallback.  This avoids
silent failures caused by REST availability/geographic restrictions.
"""
from __future__ import annotations

from io import BytesIO
from typing import Any
import json
import zipfile

import pandas as pd
import requests

from project.cryptoresearch_v1_cand000006_binance_12m_audit import (
    CryptoResearchV1Cand000006Binance12mAudit,
    FETCH_START,
    STUDY_END,
    INTERVAL,
    MIN_FETCH_BARS,
    PAIR_NAMES,
)

AUDIT_VERSION_V2 = "CRYPTORESEARCH_V1_CAND000006_BINANCE_12M_20260809_V3_FRESH"
ARCHIVE_BASE = "https://data.binance.vision/data/spot"
COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trade_count", "taker_base", "taker_quote", "ignore",
]


class CryptoResearchV1Cand000006Binance12mV2Audit(CryptoResearchV1Cand000006Binance12mAudit):
    def already_done(self) -> bool:
        self._ensure_marker()
        return bool(self.postgres.fetch_all(
            "SELECT 1 FROM statistics.velez_candidate_audit_runs WHERE version=%s LIMIT 1",
            (AUDIT_VERSION_V2,),
        ))

    def _save_marker(self, summary: dict[str, Any]) -> None:
        self.postgres.execute(
            "INSERT INTO statistics.velez_candidate_audit_runs(version,summary) VALUES (%s,%s::jsonb) ON CONFLICT (version) DO NOTHING",
            (AUDIT_VERSION_V2, json.dumps(summary, default=str)),
        )

    @staticmethod
    def _archive_urls(symbol: str) -> list[str]:
        urls: list[str] = []
        # Warm-up July 2025 through the last complete month July 2026.
        for period in pd.period_range("2025-07", "2026-07", freq="M"):
            ym = period.strftime("%Y-%m")
            urls.append(
                f"{ARCHIVE_BASE}/monthly/klines/{symbol}/{INTERVAL}/{symbol}-{INTERVAL}-{ym}.zip"
            )
        # Study ends at 2026-08-09 00:00 UTC, so only Aug 1..8 are required.
        for day in pd.date_range("2026-08-01", "2026-08-08", freq="D", tz="UTC"):
            ymd = day.strftime("%Y-%m-%d")
            urls.append(
                f"{ARCHIVE_BASE}/daily/klines/{symbol}/{INTERVAL}/{symbol}-{INTERVAL}-{ymd}.zip"
            )
        return urls

    @staticmethod
    def _read_zip(content: bytes) -> pd.DataFrame:
        with zipfile.ZipFile(BytesIO(content)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise RuntimeError("BINANCE_ARCHIVE_NO_CSV")
            with zf.open(names[0]) as fh:
                raw = pd.read_csv(fh, header=None)
        if raw.shape[1] < 6:
            raise RuntimeError(f"BINANCE_ARCHIVE_BAD_COLUMNS columns={raw.shape[1]}")
        raw = raw.iloc[:, :12]
        raw.columns = COLUMNS[: raw.shape[1]]
        ts = pd.to_numeric(raw["open_time"], errors="coerce")
        valid = ts.dropna()
        if valid.empty:
            raise RuntimeError("BINANCE_ARCHIVE_BAD_TIMESTAMP")
        # Binance public SPOT archives use microseconds from 2025 onward.
        unit = "us" if float(valid.median()) > 1e14 else "ms"
        raw["timestamp"] = pd.to_datetime(ts, unit=unit, utc=True, errors="coerce")
        for col in ("open", "high", "low", "close", "volume"):
            raw[col] = pd.to_numeric(raw[col], errors="coerce")
        return raw[["timestamp", "open", "high", "low", "close", "volume"]].dropna()

    def _fetch_from_archives(self, symbol: str) -> tuple[pd.DataFrame, dict[str, Any]]:
        frames: list[pd.DataFrame] = []
        session = requests.Session()
        session.headers.update({"User-Agent": "crypto-research-cand000006/2.0"})
        urls = self._archive_urls(symbol)
        for url in urls:
            response = session.get(url, timeout=30)
            if response.status_code != 200:
                raise RuntimeError(
                    f"BINANCE_ARCHIVE_HTTP symbol={symbol} status={response.status_code} file={url.rsplit('/',1)[-1]}"
                )
            frames.append(self._read_zip(response.content))

        frame = pd.concat(frames, ignore_index=True)
        frame = frame[(frame["timestamp"] >= FETCH_START) & (frame["timestamp"] < STUDY_END)]
        frame = frame.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
        if len(frame) < MIN_FETCH_BARS:
            raise RuntimeError(f"BINANCE_ARCHIVE_HISTORY_TOO_SHORT symbol={symbol} bars={len(frame)}")
        diffs = frame["timestamp"].diff().dropna().dt.total_seconds().div(900.0)
        missing = int(sum(max(0, int(round(v)) - 1) for v in diffs if v > 1.0))
        info = {
            "symbol": symbol,
            "pair": PAIR_NAMES[symbol],
            "bars": len(frame),
            "from": str(frame.iloc[0]["timestamp"]),
            "to": str(frame.iloc[-1]["timestamp"]),
            "requests": len(urls),
            "missing_intervals": missing,
            "transport": "BINANCE_VISION_ARCHIVES",
        }
        return frame, info

    def _fetch_symbol(self, symbol: str) -> tuple[pd.DataFrame, dict[str, Any]]:
        archive_error: Exception | None = None
        try:
            return self._fetch_from_archives(symbol)
        except Exception as exc:
            archive_error = exc
        try:
            frame, info = super()._fetch_symbol(symbol)
            info["transport"] = "BINANCE_REST_FALLBACK"
            info["archive_error"] = str(archive_error)[:240]
            return frame, info
        except Exception as rest_exc:
            raise RuntimeError(
                f"BINANCE_DATA_UNAVAILABLE symbol={symbol} archive={archive_error!r} rest={rest_exc!r}"
            ) from rest_exc

    def run(self) -> dict[str, Any]:
        summary = super().run()
        if not summary.get("skipped"):
            summary["version"] = AUDIT_VERSION_V2
            summary["data_acquisition"] = "Binance Vision archives first; REST fallback"
        return summary
