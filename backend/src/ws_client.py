"""Polymarket CLOB WebSocket client with auto-reconnect."""
import asyncio
import json
import logging
from collections import defaultdict
from typing import Callable, Awaitable
import websockets
from websockets.exceptions import ConnectionClosed

from .config_loader import CONFIG
from .logger import log, write_event
from . import volatility

WS_URL = CONFIG["polymarket"]["clob_ws_url"]

# orderbook state: asset_id -> {"bids": {price: size}, "asks": {price: size}}
_orderbooks: dict[str, dict] = defaultdict(lambda: {"bids": {}, "asks": {}})
# listeners: asset_id -> list of async callbacks
_listeners: dict[str, list[Callable]] = defaultdict(list)
_ws_task: asyncio.Task | None = None
_connected = False
_subscribed_assets: set[str] = set()


def get_orderbook(asset_id: str) -> dict:
    return _orderbooks[asset_id]


def get_best_bid(asset_id: str) -> float | None:
    bids = _orderbooks[asset_id]["bids"]
    if not bids:
        return None
    return max(float(p) for p in bids.keys())


def get_best_ask(asset_id: str) -> float | None:
    asks = _orderbooks[asset_id]["asks"]
    if not asks:
        return None
    return min(float(p) for p in asks.keys())


def get_mid_price(asset_id: str) -> float | None:
    bid = get_best_bid(asset_id)
    ask = get_best_ask(asset_id)
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def add_listener(asset_id: str, callback: Callable[..., Awaitable]) -> None:
    _listeners[asset_id].append(callback)


def remove_listener(asset_id: str, callback: Callable) -> None:
    if callback in _listeners[asset_id]:
        _listeners[asset_id].remove(callback)


def is_connected() -> bool:
    return _connected


_ws_conn = None  # reference to active websockets.WebSocketClientProtocol


async def subscribe_assets(asset_ids: list[str]) -> None:
    """Register assets and immediately subscribe if the WS is already connected."""
    new_ids = [aid for aid in asset_ids if aid not in _subscribed_assets]
    for aid in asset_ids:
        _subscribed_assets.add(aid)
    if new_ids and _connected and _ws_conn is not None:
        try:
            sub_msg = json.dumps({
                "type": "market",
                "assets_ids": new_ids,
                "custom_feature_enabled": True,
            })
            await _ws_conn.send(sub_msg)
            log.info("ws_subscribed_live", count=len(new_ids))
        except Exception as e:
            log.warning("ws_live_subscribe_failed", error=str(e))


async def _apply_book_update(asset_id: str, changes: list[dict]) -> None:
    book = _orderbooks[asset_id]
    for change in changes:
        side = change.get("side", "").lower()
        price = change.get("price")
        size = change.get("size", "0")
        if price is None:
            continue
        price_key = str(price)
        size_f = float(size)
        target = book["bids"] if side == "buy" else book["asks"]
        if size_f == 0:
            target.pop(price_key, None)
        else:
            target[price_key] = size_f

    mid = get_mid_price(asset_id)
    if mid is not None:
        volatility.on_price_update(asset_id, mid)

    # Notify listeners
    for cb in _listeners[asset_id]:
        try:
            await cb(asset_id, _orderbooks[asset_id])
        except Exception as e:
            log.error("listener_error", asset_id=asset_id, error=str(e))


async def _handle_message(msg: str) -> None:
    try:
        data = json.loads(msg)
    except json.JSONDecodeError:
        return

    if isinstance(data, list):
        for item in data:
            await _handle_single(item)
    elif isinstance(data, dict):
        await _handle_single(data)


async def _handle_single(data: dict) -> None:
    event_type = data.get("event_type") or data.get("type")
    asset_id = data.get("asset_id")

    if event_type == "book" and asset_id:
        _orderbooks[asset_id] = {"bids": {}, "asks": {}}
        for entry in data.get("bids", []):
            p = str(entry.get("price"))
            s = float(entry.get("size", 0))
            if s > 0:
                _orderbooks[asset_id]["bids"][p] = s
        for entry in data.get("asks", []):
            p = str(entry.get("price"))
            s = float(entry.get("size", 0))
            if s > 0:
                _orderbooks[asset_id]["asks"][p] = s
        mid = get_mid_price(asset_id)
        if mid is not None:
            volatility.on_price_update(asset_id, mid)
        for cb in _listeners[asset_id]:
            try:
                await cb(asset_id, _orderbooks[asset_id])
            except Exception as e:
                log.error("listener_error", asset_id=asset_id, error=str(e))

    elif event_type == "price_change" and asset_id:
        changes = data.get("changes", [])
        await _apply_book_update(asset_id, changes)

    elif event_type == "last_trade_price":
        pass  # ignore


async def _run_ws() -> None:
    global _connected, _ws_conn
    backoff = 1
    while True:
        try:
            log.info("ws_connecting", url=WS_URL)
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=10) as ws:
                _ws_conn = ws
                _connected = True
                backoff = 1
                log.info("ws_connected")

                if _subscribed_assets:
                    sub_msg = json.dumps({
                        "type": "market",
                        "assets_ids": list(_subscribed_assets),
                        "custom_feature_enabled": True,
                    })
                    await ws.send(sub_msg)

                async for raw in ws:
                    await _handle_message(raw)

        except ConnectionClosed as e:
            _connected = False
            _ws_conn = None
            log.warning("ws_disconnected", reason=str(e))
        except Exception as e:
            _connected = False
            _ws_conn = None
            log.error("ws_error", error=str(e))

        await asyncio.sleep(min(backoff, 30))
        backoff = min(backoff * 2, 30)


async def start() -> None:
    global _ws_task
    if _ws_task is None or _ws_task.done():
        _ws_task = asyncio.create_task(_run_ws())
        log.info("ws_client_started")


async def stop() -> None:
    global _ws_task, _connected
    if _ws_task:
        _ws_task.cancel()
        try:
            await _ws_task
        except asyncio.CancelledError:
            pass
    _connected = False
    log.info("ws_client_stopped")
