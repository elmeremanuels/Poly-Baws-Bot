"""Order placement, cancellation, and signing via py-clob-client-v2 (CLOB V2)."""
import asyncio
from typing import Any
from functools import partial

from .config_loader import CONFIG, get_env
from .logger import log

CLOB_REST = CONFIG["polymarket"]["clob_rest_url"]
CHAIN_ID = CONFIG["polymarket"]["chain_id"]

_client = None


def _sig_type(proxy: str | None) -> int:
    """Resolve the EIP-712 signature type for the deposit wallet.

    Polymarket wallet types:
      0 = EOA            (no proxy; signing key holds the funds directly)
      1 = POLY_PROXY     (email/Magic login deposit wallet)
      2 = POLY_GNOSIS_SAFE (browser-wallet / MetaMask login deposit wallet)

    Override via POLYMARKET_SIGNATURE_TYPE. Default: 1 when a proxy is set
    (email/Magic POLY_PROXY is the most common deposit wallet); Gnosis Safe
    users must set POLYMARKET_SIGNATURE_TYPE=2 explicitly. Else 0 (bare EOA).
    """
    explicit = get_env("POLYMARKET_SIGNATURE_TYPE")
    if explicit is not None and explicit.strip() != "":
        return int(explicit)
    return 1 if proxy else 0


def _build_client():
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds

    pk = get_env("POLYMARKET_PRIVATE_KEY")
    proxy = get_env("POLYMARKET_PROXY_ADDRESS")
    if not pk:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY not set")

    sig_type = _sig_type(proxy)

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
        log.info("clob_client_level2_explicit", signature_type=sig_type)
    else:
        # Derive Level 2 credentials from the private key (deterministic, no manual setup needed)
        l1 = ClobClient(
            host=CLOB_REST,
            chain_id=CHAIN_ID,
            key=pk,
            signature_type=sig_type,
            funder=proxy,
        )
        creds = l1.create_or_derive_api_key()
        log.info("clob_client_level2_derived", signature_type=sig_type)

    return ClobClient(
        host=CLOB_REST,
        chain_id=CHAIN_ID,
        key=pk,
        signature_type=sig_type,
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


def _is_maker_not_allowed(e: Exception) -> bool:
    return "maker address not allowed" in str(e).lower()


def _mask(s: str | None) -> str:
    if not s:
        return "(none)"
    return f"{s[:4]}…{s[-4:]}" if len(s) > 8 else "(set)"


async def check_credentials() -> bool:
    try:
        proxy = get_env("POLYMARKET_PROXY_ADDRESS")
        pk = get_env("POLYMARKET_PRIVATE_KEY")
        if pk:
            from eth_account import Account
            eoa = Account.from_key(pk).address
        else:
            eoa = "(no key)"
        explicit_key = get_env("POLYMARKET_API_KEY")
        log.info("wallet_info",
                 eoa=eoa,
                 proxy=proxy or "(not set)",
                 maker_address=proxy if proxy else eoa,
                 signature_type=_sig_type(proxy),
                 explicit_api_creds=bool(explicit_key),
                 api_key=_mask(explicit_key))
        client = get_client()
        # Actively verify L2 auth: get_api_keys lists keys registered to POLY_ADDRESS (the EOA).
        # A 401 here means the api creds in use don't belong to the EOA we're signing as.
        try:
            keys = await _run_sync(client.get_api_keys)
            log.info("api_keys_verified", registered=keys)
        except Exception as auth_e:
            log.critical("api_key_auth_failed",
                         error=str(auth_e),
                         fix="If POLYMARKET_API_KEY/SECRET/PASSPHRASE are set in .env, remove "
                             "them so the bot derives the EOA's own key. Explicit creds generated "
                             "via the Polymarket website belong to the proxy, not the signing EOA.")
            return False
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
        if _is_maker_not_allowed(e):
            log.critical("maker_not_allowed",
                         token_id=token_id, side=side, price=price, size=size,
                         fix="Complete Polymarket deposit wallet flow at polymarket.com, "
                             "then set POLYMARKET_PROXY_ADDRESS to your proxy wallet address")
        else:
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
        if _is_maker_not_allowed(e):
            log.critical("maker_not_allowed",
                         token_id=token_id, side=side, size=size,
                         fix="Complete Polymarket deposit wallet flow at polymarket.com, "
                             "then set POLYMARKET_PROXY_ADDRESS to your proxy wallet address")
        else:
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
    """Get USDC balance.

    Primary: CLOB /balance-allowance (uses existing wallet credentials, no KYC).
    Fallback: Polygon RPC (no credentials needed at all).
    """
    # Primary: CLOB balance-allowance — same credentials as order placement
    try:
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        client = get_client()
        result = await _run_sync(
            client.get_balance_allowance,
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
        )
        if result and "balance" in result:
            # CLOB returns raw USDC units (6 decimals), same as on-chain ERC-20
            return float(result["balance"]) / 1e6
    except Exception as e:
        log.warning("get_balance_clob_failed", error=str(e))

    # Fallback: Polygon RPC (public endpoint, no credentials)
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
