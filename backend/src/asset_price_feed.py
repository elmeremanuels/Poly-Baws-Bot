"""Real-time underlying asset price feed via Kraken REST polling.

Binance is geo-blocked in DE/EU (HTTP 451 / CloudFront restriction).
Kraken is accessible and provides equivalent OHLC + ticker data.

Strategy:
  - Seed: 30 minutes of 1-minute OHLC on startup via Kraken /OHLC
  - Live:  poll /Ticker for all coins every 5 seconds (one request)
"""
from __future__ import annotations

import asyncio

import httpx

from .config_loader import CONFIG
from .logger import log
from . import regime as _regime

_COIN_TO_SYMBOL: dict[str, str] = {
    "BTC":  "XBTUSD",
    "ETH":  "ETHUSD",
    "SOL":  "SOLUSD",
    "XRP":  "XRPUSD",
    "DOGE": "XDGEUSD",
}

# Kraken returns result keys in "long form" (XXBTZUSD, XETHZUSD, etc.)
# Map both long and short forms back to coin names.
_KRAKEN_TO_COIN: dict[str, str] = {
    "XXBTZUSD": "BTC", "XBTUSD":  "BTC",
    "XETHZUSD": "ETH", "ETHUSD":  "ETH",
    "SOLUSD":   "SOL",
    "XXRPZUSD": "XRP", "XRPUSD":  "XRP",
    "XDGEUSD":  "DOGE",
}

_REST_BASE     = "https://api.kraken.com/0/public"
_POLL_INTERVAL = 5.0


async def _seed_prices() -> None:
    """Backfill 30 minutes of 1-minute Kraken OHLC before polling starts."""
    import collections
    coins = [c for c in CONFIG.get("coins", {}).keys() if c in _COIN_TO_SYMBOL]
    async with httpx.AsyncClient(timeout=15) as client:
        for coin in coins:
            pair = _COIN_TO_SYMBOL[coin]
            try:
                resp = await client.get(
                    f"{_REST_BASE}/OHLC",
                    params={"pair": pair, "interval": 1},
                )
                resp.raise_for_status()
                result = resp.json().get("result", {})
                candles = next((v for k, v in result.items() if k != "last"), [])
                for kline in candles[-30:]:
                    # Kraken OHLC: [time, open, high, low, close, vwap, volume, count]
                    ts          = float(kline[0])   # Unix seconds (already correct)
                    close_price = float(kline[4])
                    buf = _regime._asset_prices.setdefault(coin, collections.deque())
                    buf.append((ts, close_price))
                log.info("asset_price_seeded", coin=coin, n=min(30, len(candles)), source="kraken")
            except Exception as e:
                log.warning("asset_price_seed_failed", coin=coin, error=str(e))
            await asyncio.sleep(0.3)  # Kraken public rate limit


async def run() -> None:
    """Seed price buffer, then poll Kraken /Ticker every 5 seconds.

    One REST call fetches all coins at once.
    Exponential backoff on errors (2s → 4s → … cap 60s).
    """
    await _seed_prices()

    coins   = [c for c in CONFIG.get("coins", {}).keys() if c in _COIN_TO_SYMBOL]
    pairs   = ",".join(_COIN_TO_SYMBOL[c] for c in coins)

    log.info("asset_price_feed_starting", mode="kraken_rest_poll", interval=_POLL_INTERVAL)
    backoff = 2.0

    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                backoff = 2.0
                while True:
                    try:
                        resp = await client.get(
                            f"{_REST_BASE}/Ticker",
                            params={"pair": pairs},
                        )
                        resp.raise_for_status()
                        result = resp.json().get("result", {})
                        for kraken_key, data in result.items():
                            coin = _KRAKEN_TO_COIN.get(kraken_key)
                            if coin and coin in CONFIG.get("coins", {}):
                                # "c" = [last_trade_price, last_trade_volume]
                                _regime.record_asset_price(coin, float(data["c"][0]))
                    except Exception as e:
                        log.warning("asset_price_poll_error", error=str(e))
                    await asyncio.sleep(_POLL_INTERVAL)
        except Exception as e:
            log.warning("asset_price_feed_error", error=str(e), retry_in=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
