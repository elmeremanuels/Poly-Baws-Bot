"""Real-time underlying asset price feed via Binance REST polling.

WebSocket streams (wss://stream.binance.com) are blocked for datacenter IPs
(HTTP 451). The REST API works fine from the same server.

Strategy:
  - Seed: 30 minutes of 1-minute klines on startup (existing, unchanged)
  - Live:  poll /api/v3/ticker/price for all coins every 5 seconds (one request)
"""
from __future__ import annotations

import asyncio
import json

import httpx

from .config_loader import CONFIG
from .logger import log
from . import regime as _regime

_COIN_TO_SYMBOL: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
    "DOGE": "DOGEUSDT",
}

_REST_BASE = "https://api.binance.com/api/v3"
_POLL_INTERVAL = 5.0  # seconds between price polls


async def _seed_prices() -> None:
    """Backfill 30 minutes of 1-minute klines from Binance REST before polling starts."""
    coins = [c for c in CONFIG.get("coins", {}).keys() if c in _COIN_TO_SYMBOL]
    async with httpx.AsyncClient(timeout=15) as client:
        for coin in coins:
            symbol = _COIN_TO_SYMBOL[coin]
            try:
                resp = await client.get(
                    f"{_REST_BASE}/klines",
                    params={"symbol": symbol, "interval": "1m", "limit": 30},
                )
                resp.raise_for_status()
                klines = resp.json()
                for kline in klines:
                    ts = int(kline[0]) / 1000.0
                    close_price = float(kline[4])
                    buf = _regime._asset_prices.setdefault(
                        coin, __import__("collections").deque()
                    )
                    buf.append((ts, close_price))
                log.info("asset_price_seeded", coin=coin, n=len(klines))
            except Exception as e:
                log.warning("asset_price_seed_failed", coin=coin, error=str(e))


async def run() -> None:
    """Seed price buffer, then poll Binance ticker/price every 5 seconds.

    One REST call fetches all coins at once.
    Exponential backoff on errors (2s → 4s → … cap 60s).
    """
    await _seed_prices()

    coins = [c for c in CONFIG.get("coins", {}).keys() if c in _COIN_TO_SYMBOL]
    symbols = json.dumps([_COIN_TO_SYMBOL[c] for c in coins], separators=(",", ":"))
    upper_map = {v: k for k, v in _COIN_TO_SYMBOL.items()}

    log.info("asset_price_feed_starting", mode="rest_poll", interval=_POLL_INTERVAL)
    backoff = 2.0

    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                backoff = 2.0
                while True:
                    try:
                        resp = await client.get(
                            f"{_REST_BASE}/ticker/price",
                            params={"symbols": symbols},
                        )
                        resp.raise_for_status()
                        for item in resp.json():
                            coin = upper_map.get(item.get("symbol", ""))
                            if coin and coin in CONFIG.get("coins", {}):
                                _regime.record_asset_price(coin, float(item["price"]))
                    except Exception as e:
                        log.warning("asset_price_poll_error", error=str(e))
                    await asyncio.sleep(_POLL_INTERVAL)
        except Exception as e:
            log.warning("asset_price_feed_error", error=str(e), retry_in=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
