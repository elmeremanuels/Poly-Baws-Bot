"""Real-time underlying asset price feed via Binance WebSocket miniTicker streams.

Calls regime.record_asset_price(coin, price) on every tick so regime detection
has a continuous 30-minute rolling buffer of actual USD prices.

On startup, seeds the buffer with 30 minutes of Binance REST klines so regime
detection works immediately instead of waiting 20+ minutes for live data.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from .config_loader import CONFIG
from .logger import log
from . import regime as _regime

_COIN_TO_SYMBOL: dict[str, str] = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
    "XRP": "xrpusdt",
    "DOGE": "dogeusdt",
}

_WS_BASE = "wss://stream.binance.com:9443/stream?streams="
_BINANCE_REST = "https://api.binance.com/api/v3/klines"


def _build_url() -> str:
    coins = list(CONFIG.get("coins", {}).keys())
    symbols = [_COIN_TO_SYMBOL[c] for c in coins if c in _COIN_TO_SYMBOL]
    streams = "/".join(f"{s}@miniTicker" for s in symbols)
    return _WS_BASE + streams


async def _seed_prices() -> None:
    """Backfill 30 minutes of 1-minute klines from Binance REST before WebSocket starts."""
    coins = [c for c in CONFIG.get("coins", {}).keys() if c in _COIN_TO_SYMBOL]
    async with httpx.AsyncClient(timeout=15) as client:
        for coin in coins:
            symbol = _COIN_TO_SYMBOL[coin].upper()
            try:
                resp = await client.get(
                    _BINANCE_REST,
                    params={"symbol": symbol, "interval": "1m", "limit": 30},
                )
                resp.raise_for_status()
                klines = resp.json()
                # kline format: [open_time, open, high, low, close, ...]
                for kline in klines:
                    open_time_ms = int(kline[0])
                    close_price = float(kline[4])
                    ts = open_time_ms / 1000.0
                    buf = _regime._asset_prices.setdefault(coin, __import__("collections").deque())
                    buf.append((ts, close_price))
                log.info("asset_price_seeded", coin=coin, n=len(klines))
            except Exception as e:
                log.warning("asset_price_seed_failed", coin=coin, error=str(e))


async def run() -> None:
    """Seed price buffer, then connect to Binance combined stream.

    Reconnects automatically with exponential backoff (2s → 4s → 8s … cap 60s).
    """
    import websockets

    await _seed_prices()

    url = _build_url()
    backoff = 2.0
    log.info("asset_price_feed_starting", url=url)

    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=10,
                open_timeout=15,
            ) as ws:
                backoff = 2.0  # reset on successful connect
                log.info("asset_price_feed_connected")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        data = msg.get("data", {})
                        symbol = (data.get("s") or "").upper()  # e.g. "BTCUSDT"
                        price_str = data.get("c")
                        if not price_str:
                            continue
                        # Reverse lookup: "BTCUSDT" → "BTC"
                        coin = next(
                            (c for c, s in _COIN_TO_SYMBOL.items() if s.upper() == symbol),
                            None,
                        )
                        if coin and coin in CONFIG.get("coins", {}):
                            _regime.record_asset_price(coin, float(price_str))
                    except Exception as e:
                        log.warning("asset_price_feed_parse_error", error=str(e))
        except Exception as e:
            log.warning("asset_price_feed_disconnected", error=str(e), reconnect_in=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
