"""Real-time underlying asset price feed via Binance WebSocket miniTicker streams.

Calls regime.record_asset_price(coin, price) on every tick so regime detection
has a continuous 30-minute rolling buffer of actual USD prices.
"""
from __future__ import annotations

import asyncio
import json
import time

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


def _build_url() -> str:
    coins = list(CONFIG.get("coins", {}).keys())
    symbols = [_COIN_TO_SYMBOL[c] for c in coins if c in _COIN_TO_SYMBOL]
    streams = "/".join(f"{s}@miniTicker" for s in symbols)
    return _WS_BASE + streams


async def run() -> None:
    """Connect to Binance combined stream and feed prices to regime module.

    Reconnects automatically with exponential backoff (2s → 4s → 8s … cap 60s).
    """
    import websockets

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
