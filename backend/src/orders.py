"""Order placement, cancellation, and signing via py-clob-client-v2 (CLOB V2)."""
import asyncio
from typing import Any
from functools import partial

from .config_loader import CONFIG, get_env
from .logger import log

CLOB_REST = CONFIG["polymarket"]["clob_rest_url"]
CHAIN_ID = CONFIG["polymarket"]["chain_id"]

_client = None


def _build_client():
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds

    pk = get_env("POLYMARKET_PRIVATE_KEY")
    proxy = get_env("POLYMARKET_PROXY_ADDRESS")
    if not pk:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY not set")

    # Prefer explicit API credentials from environment
    api_key = get_env("POLYMARKET_API_KEY")
    api_secret = get_env("POLYMARKET_API_SECRET")
    api_passphrase = get_env("POLYMARKET_API_PASSPHRASE")

    if api_key and api_secret and api_passphrase:
        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
        log.info("clob_client_level2_explicit")
    else:
        # Derive Level 2 credentials from the private key (deterministic, no manual setup needed)
        l1 = ClobClient(
            host=CLOB_REST,
            chain_id=CHAIN_ID,
            key=pk,
            signature_type=1 if proxy else 0,
            funder=proxy,
        )
        creds = l1.create_or_derive_api_key()
        log.info("clob_client_level2_derived")

    return ClobClient(
        host=CLOB_REST,
        chain_id=CHAIN_ID,
        key=pk,
        signature_type=1 if proxy else 0,
        funder=proxy,
        creds=creds,
    )


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
    from py_clob_client_v2.clob_types import OrderArgsV2, OrderType
    try:
        client = get_client()
        order_args = OrderArgsV2(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )
        resp = await _run_sync(client.create_and_post_order, order_args, None, OrderType.GTC)
        order_id = resp.get("orderID") or resp.get("order_id")
        if not order_id:
            log.error("order_placed_no_id", token_id=token_id, side=side, price=price, resp=resp)
        log.info("order_placed", token_id=token_id, side=side, price=price, size=size, order_id=order_id)
        return {"order_id": order_id, "status": resp.get("status"), "raw": resp}
    except Exception as e:
        log.error("place_order_failed", token_id=token_id, side=side, price=price, size=size, error=str(e))
        return None


async def place_market_order(token_id: str, side: str, size: float) -> dict | None:
    from py_clob_client_v2.clob_types import MarketOrderArgsV2, OrderType
    try:
        client = get_client()
        order_args = MarketOrderArgsV2(
            token_id=token_id,
            amount=size,
            side=side,
        )
        resp = await _run_sync(client.create_and_post_market_order, order_args, None, OrderType.FOK)
        order_id = resp.get("orderID") or resp.get("order_id")
        if not order_id:
            log.error("market_order_no_id", token_id=token_id, side=side, size=size, resp=resp)
        log.info("market_order_placed", token_id=token_id, side=side, size=size, order_id=order_id)
        return {"order_id": order_id, "status": resp.get("status"), "raw": resp}
    except Exception as e:
        log.error("market_order_failed", token_id=token_id, side=side, size=size, error=str(e))
        return None


async def cancel_order(order_id: str) -> bool:
    from py_clob_client_v2.clob_types import OrderPayload
    try:
        client = get_client()
        resp = await _run_sync(client.cancel_order, OrderPayload(orderID=order_id))
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
    """Get USDC balance on Polygon via public RPC — no CLOB Level 2 auth needed."""
    try:
        import httpx as _httpx
        from eth_account import Account
        proxy = get_env("POLYMARKET_PROXY_ADDRESS")
        if proxy:
            address = proxy
        else:
            pk = get_env("POLYMARKET_PRIVATE_KEY")
            address = Account.from_key(pk).address
        usdc = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        # balanceOf(address) selector
        data = "0x70a08231" + "000000000000000000000000" + address[2:].lower()
        async with _httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(
                "https://polygon-rpc.com",
                json={"jsonrpc": "2.0", "method": "eth_call",
                      "params": [{"to": usdc, "data": data}, "latest"], "id": 1},
            )
            result = resp.json().get("result", "0x0") or "0x0"
            return int(result, 16) / 1e6
    except Exception as e:
        log.error("get_balance_failed", error=str(e))
        return None
