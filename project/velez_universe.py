"""Resolve the top liquid non-stable cryptocurrencies by market cap.

CoinGecko provides the ranking; Kraken AssetPairs is used to keep only USD markets
that the existing collector can actually download. A conservative fallback keeps
the service operational if either public endpoint is temporarily unavailable.
"""
from __future__ import annotations

import os
from typing import Any

import requests

COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"
KRAKEN_PAIRS_URL = "https://api.kraken.com/0/public/AssetPairs"

STABLE_SYMBOLS = {
    "USDT", "USDC", "DAI", "USDE", "USDS", "FDUSD", "TUSD", "PYUSD",
    "USD1", "BUSD", "GUSD", "FRAX", "LUSD", "RLUSD",
}
WRAPPED_SYMBOLS = {"WBTC", "WETH", "STETH", "WSTETH", "WEETH", "CBBTC"}

FALLBACK = [
    "BTC/USD", "ETH/USD", "XRP/USD", "SOL/USD", "DOGE/USD",
    "ADA/USD", "TRX/USD", "AVAX/USD", "LINK/USD", "BCH/USD",
    "XLM/USD", "LTC/USD", "DOT/USD", "AAVE/USD", "UNI/USD",
    "ETC/USD", "ATOM/USD", "FIL/USD", "NEAR/USD", "TAO/USD",
]


def _kraken_usd_symbols(timeout: int = 10) -> set[str]:
    response = requests.get(KRAKEN_PAIRS_URL, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    symbols: set[str] = set()
    for item in (payload.get("result") or {}).values():
        wsname = str(item.get("wsname") or "")
        if not wsname.endswith("/USD"):
            continue
        symbols.add(wsname.split("/", 1)[0].upper())
    return symbols


def resolve_top_market_cap_pairs(limit: int = 20, timeout: int = 10) -> list[str]:
    """Return top market-cap, non-stable, non-wrapped coins tradable as Kraken/USD."""
    limit = max(5, int(limit))
    try:
        kraken_symbols = _kraken_usd_symbols(timeout=timeout)
        response = requests.get(
            os.getenv("COINGECKO_MARKETS_URL", COINGECKO_URL),
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 100,
                "page": 1,
                "sparkline": "false",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        markets: list[dict[str, Any]] = list(response.json() or [])
        pairs: list[str] = []
        for coin in markets:
            symbol = str(coin.get("symbol") or "").upper()
            if not symbol or symbol in STABLE_SYMBOLS or symbol in WRAPPED_SYMBOLS:
                continue
            if symbol not in kraken_symbols:
                continue
            pair = f"{symbol}/USD"
            if pair not in pairs:
                pairs.append(pair)
            if len(pairs) >= limit:
                return pairs
    except Exception:
        pass
    return FALLBACK[:limit]
