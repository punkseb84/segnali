"""Minimal client for Hyperliquid's public info endpoint.

Only public read operations are implemented. The client never signs requests and never
places orders. Time-range pagination follows the official ``userFillsByTime`` endpoint
and defensively deduplicates fills because several fills may share the same timestamp.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
import time
from typing import Any, Callable, Iterable

import requests


INFO_URL = "https://api.hyperliquid.xyz/info"
_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


class HyperliquidApiError(RuntimeError):
    """Raised when a public Hyperliquid request cannot be completed safely."""


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class HyperliquidFill:
    wallet: str
    coin: str
    side: str
    direction: str
    price: Decimal
    size: Decimal
    start_position: Decimal
    closed_pnl: Decimal
    fee: Decimal
    fee_token: str
    time_ms: int
    crossed: bool
    tx_hash: str
    order_id: str
    trade_id: str
    raw: dict[str, Any]

    @property
    def time(self) -> datetime:
        return datetime.fromtimestamp(self.time_ms / 1000.0, tz=timezone.utc)

    @property
    def signed_size(self) -> Decimal:
        return self.size if self.side.upper() == "B" else -self.size

    @property
    def end_position(self) -> Decimal:
        return self.start_position + self.signed_size

    @property
    def fill_key(self) -> str:
        # ``tid`` is normally unique, but older/aggregated responses are safer when the
        # transaction and economic fields are part of the key as well.
        return "|".join(
            [
                self.wallet.lower(),
                self.trade_id,
                self.tx_hash,
                self.coin,
                str(self.time_ms),
                str(self.price),
                str(self.size),
                self.side,
            ]
        )

    @classmethod
    def from_api(cls, wallet: str, payload: dict[str, Any]) -> "HyperliquidFill":
        return cls(
            wallet=wallet.lower(),
            coin=str(payload.get("coin") or "").strip(),
            side=str(payload.get("side") or "").strip().upper(),
            direction=str(payload.get("dir") or "").strip(),
            price=_decimal(payload.get("px")),
            size=abs(_decimal(payload.get("sz"))),
            start_position=_decimal(payload.get("startPosition")),
            closed_pnl=_decimal(payload.get("closedPnl")),
            fee=_decimal(payload.get("fee")),
            fee_token=str(payload.get("feeToken") or "USDC"),
            time_ms=_int(payload.get("time")),
            crossed=bool(payload.get("crossed", False)),
            tx_hash=str(payload.get("hash") or ""),
            order_id=str(payload.get("oid") or ""),
            trade_id=str(payload.get("tid") or ""),
            raw=dict(payload),
        )

    def is_valid(self) -> bool:
        return bool(
            self.coin
            and self.side in {"A", "B"}
            and self.price > 0
            and self.size > 0
            and self.time_ms > 0
        )


class HyperliquidInfoClient:
    """Retrying, injectable client for public account/fill information."""

    def __init__(
        self,
        *,
        base_url: str = INFO_URL,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url
        self.timeout_seconds = max(2.0, float(timeout_seconds))
        self.max_retries = max(1, int(max_retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.session = session or requests.Session()
        self.sleeper = sleeper

    @staticmethod
    def validate_address(wallet: str) -> str:
        normalized = str(wallet or "").strip().lower()
        if not _ADDRESS_RE.fullmatch(normalized):
            raise ValueError("Hyperliquid wallet must be a 42-character hexadecimal address")
        return normalized

    def _post(self, payload: dict[str, Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self.session.post(
                    self.base_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=self.timeout_seconds,
                )
                if response.status_code >= 500 or response.status_code == 429:
                    raise HyperliquidApiError(
                        f"Hyperliquid temporary HTTP status {response.status_code}"
                    )
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError, HyperliquidApiError) as exc:
                last_error = exc
                if attempt + 1 < self.max_retries:
                    self.sleeper(self.retry_backoff_seconds * (2**attempt))
        raise HyperliquidApiError(f"Hyperliquid request failed: {last_error}") from last_error

    def user_role(self, wallet: str) -> str:
        wallet = self.validate_address(wallet)
        payload = self._post({"type": "userRole", "user": wallet})
        if not isinstance(payload, dict):
            raise HyperliquidApiError("Unexpected userRole response")
        return str(payload.get("role") or "missing")

    def portfolio(self, wallet: str) -> Any:
        wallet = self.validate_address(wallet)
        return self._post({"type": "portfolio", "user": wallet})

    def fetch_user_fills_by_time(
        self,
        wallet: str,
        *,
        start_time_ms: int,
        end_time_ms: int | None = None,
        aggregate_by_time: bool = True,
        max_fills: int = 10_000,
        page_size_hint: int = 2_000,
    ) -> list[HyperliquidFill]:
        """Fetch and deduplicate the most recent public fills in a time range.

        Hyperliquid exposes only the 10,000 most recent fills and caps each response. The
        implementation advances to ``last timestamp + 1`` and also deduplicates by fill key
        so equal timestamps, retries and aggregate responses cannot duplicate observations.
        """
        wallet = self.validate_address(wallet)
        start = max(0, int(start_time_ms))
        end = int(end_time_ms) if end_time_ms is not None else int(time.time() * 1000)
        if end < start:
            raise ValueError("end_time_ms must not be earlier than start_time_ms")
        hard_limit = min(10_000, max(1, int(max_fills)))
        page_size_hint = max(1, min(2_000, int(page_size_hint)))

        fills: dict[str, HyperliquidFill] = {}
        cursor = start
        while cursor <= end and len(fills) < hard_limit:
            payload = self._post(
                {
                    "type": "userFillsByTime",
                    "user": wallet,
                    "startTime": cursor,
                    "endTime": end,
                    "aggregateByTime": bool(aggregate_by_time),
                }
            )
            if not isinstance(payload, list):
                raise HyperliquidApiError("Unexpected userFillsByTime response")
            page: list[HyperliquidFill] = []
            for raw in payload:
                if not isinstance(raw, dict):
                    continue
                fill = HyperliquidFill.from_api(wallet, raw)
                if fill.is_valid() and start <= fill.time_ms <= end:
                    page.append(fill)
                    fills[fill.fill_key] = fill
            if not page:
                break
            last_time = max(fill.time_ms for fill in page)
            if last_time < cursor:
                raise HyperliquidApiError("Fill pagination did not advance")
            next_cursor = last_time + 1
            if next_cursor <= cursor:
                raise HyperliquidApiError("Fill pagination cursor stalled")
            cursor = next_cursor
            # Responses shorter than the documented cap normally mean the range is exhausted.
            if len(payload) < page_size_hint:
                break

        ordered = sorted(
            fills.values(),
            key=lambda item: (item.time_ms, item.trade_id, item.order_id, item.fill_key),
        )
        return ordered[-hard_limit:]


def serialize_fills(fills: Iterable[HyperliquidFill]) -> list[dict[str, Any]]:
    return [
        {
            "fill_key": fill.fill_key,
            "wallet": fill.wallet,
            "coin": fill.coin,
            "side": fill.side,
            "direction": fill.direction,
            "price": str(fill.price),
            "size": str(fill.size),
            "start_position": str(fill.start_position),
            "end_position": str(fill.end_position),
            "closed_pnl": str(fill.closed_pnl),
            "fee": str(fill.fee),
            "fee_token": fill.fee_token,
            "time_ms": fill.time_ms,
            "crossed": fill.crossed,
            "tx_hash": fill.tx_hash,
            "order_id": fill.order_id,
            "trade_id": fill.trade_id,
            "raw": fill.raw,
        }
        for fill in fills
    ]
