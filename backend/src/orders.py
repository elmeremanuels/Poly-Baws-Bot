"""Order placement, cancellation, and signing via py-clob-client."""
import asyncio
from typing import Any
from functools import partial

from .config_loader import CONFIG, get_env
from .logger import log

CLOB_REST = CONFIG["polymarket"]["clob_rest_url"]
CHAIN_ID = CONFIG["polymarket"]["chain_id"]

_client = None


def _build_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    pk = get_env("POLYMARKET_PRIVATE_KEY")
    proxy = get_env("POLYMARKET_PROXY_ADDRESS")
    if not pk:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY not set")

    client = ClobClient(
        host=CLOB_REST,
        chain_id=CHAIN_ID,
        key=pk,
        signature_type=1 if proxy else 0,
        funder=proxy,
    )
    return client


def get_client():
    global _client
    if _client is None:
        _client = _build_client()
    return _client


async def _run_sync(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))


async def check_credentials() -> bool:
    try:
        client = get_client()
        await _run_sync(client.get_ok)
        return True
    except Exception as e:
        log.error("credentials_check_failed", error=str(e))
        return False


async def get_orderbook(token_id: str) -> dict | None:
    try:
        client = get_client()
        book = await _run_sync(client.get_order_book, token_id)
        return {
            "bids": [{"price": b.price, "size": b.size} for b in (book.bids or [])],
            "asks": [{"price": a.price, "size": a.size} for a in (book.asks or [])],
        }
    except Exception as e:
        log.error("get_orderbook_failed", token_id=token_id, error=str(e))
        return None


async def get_midprice(token_id: str) -> float | None:
    book = await get_orderbook(token_id)
    if not book:
        return None
    bids = book["bids"]
    asks = book["asks"]
    if not bids or not asks:
        return None
    best_bid = max(float(b["price"]) for b in bids)
    best_ask = min(float(a["price"]) for a in asks)
    return (best_bid + best_ask) / 2


async def place_limit_order(
    token_id: str,
    side: str,  # "BUY" or "SELL"
    price: float,
    size: float,
) -> dict | None:
    from py_clob_client.clob_types import OrderArgs, OrderType
    try:
        client = get_client()
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )
        signed = await _run_sync(client.create_order, order_args)
        resp = await _run_sync(client.post_order, signed, OrderType.GTC)
        order_id = resp.get("orderID") or resp.get("order_id")
        log.info("order_placed", token_id=token_id, side=side, price=price, size=size, order_id=order_id)
        return {"order_id": order_id, "status": resp.get("status"), "raw": resp}
    except Exception as e:
        log.error("place_order_failed", token_id=token_id, side=side, price=price, size=size, error=str(e))
        return None


async def place_market_order(token_id: str, side: str, size: float) -> dict | None:
    from py_clob_client.clob_types import OrderArgs, OrderType
    try:
        client = get_client()
        order_args = OrderArgs(
            token_id=token_id,
            price=None,
            size=size,
            side=side,
        )
        signed = await _run_sync(client.create_market_order, order_args)
        resp = await _run_sync(client.post_order, signed, OrderType.FOK)
        order_id = resp.get("orderID") or resp.get("order_id")
        log.info("market_order_placed", token_id=token_id, side=side, size=size, order_id=order_id)
        return {"order_id": order_id, "status": resp.get("status"), "raw": resp}
    except Exception as e:
        log.error("market_order_failed", token_id=token_id, side=side, size=size, error=str(e))
        return None


async def cancel_order(order_id: str) -> bool:
    try:
        client = get_client()
        resp = await _run_sync(client.cancel, order_id)
        log.info("order_cancelled", order_id=order_id)
        return True
    except Exception as e:
        log.error("cancel_order_failed", order_id=order_id, error=str(e))
        return False


async def cancel_all_orders() -> bool:
    try:
        client = get_client()
        await _run_sync(client.cancel_all)
        log.info("all_orders_cancelled")
        return True
    except Exception as e:
        log.error("cancel_all_failed", error=str(e))
        return False


async def get_order(order_id: str) -> dict | None:
    try:
        client = get_client()
        resp = await _run_sync(client.get_order, order_id)
        return resp
    except Exception as e:
        log.error("get_order_failed", order_id=order_id, error=str(e))
        return None


async def get_open_positions() -> list[dict]:
    """Fetch current token positions via Polymarket Data API (public, no auth needed)."""
    try:
        import httpx as _httpx
        client = get_client()
        proxy = get_env("POLYMARKET_PROXY_ADDRESS")
        address = proxy or (client.signer.address() if client.signer else None)
        if not address:
            return []
        async with _httpx.AsyncClient(timeout=30) as http:
            resp = await http.get(
                "https://data-api.polymarket.com/positions",
                params={"user": address, "sizeThreshold": "0.01"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        log.error("get_positions_failed", error=str(e))
        return []


async def get_balance() -> float | None:
    """Fetch USDC collateral balance via CLOB API."""
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        client = get_client()
        proxy = get_env("POLYMARKET_PROXY_ADDRESS")
        sig_type = 1 if proxy else 0
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=sig_type,
        )
        resp = await _run_sync(client.get_balance_allowance, params)
        bal = resp.get("balance")
        return float(bal) / 1e6 if bal is not None else None
    except Exception as e:
        log.error("get_balance_failed", error=str(e))
        return None
