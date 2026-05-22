"""Paper trading simulation engine — realistic fill simulation against live orderbook."""
import asyncio
from datetime import datetime, timezone
from typing import Any

from . import ws_client
from .config_loader import CONFIG
from .logger import log

_FEES_CFG = CONFIG["fees"]
SLIPPAGE_BUFFER: float = _FEES_CFG["paper_slippage_per_share"]


def taker_fee_rate(fill_price: float) -> float:
    """
    Dynamic taker fee rate based on fill probability.
    Peaks at CONFIG fees.peak_rate_pct near p=0.50, scales linearly to 0 at p=0 and p=1.
    Maker orders are zero fee — this function is only called for taker (market / crossing) orders.
    """
    p = max(0.0, min(1.0, fill_price))
    peak = _FEES_CFG["peak_rate_pct"] / 100.0
    return peak * min(p, 1.0 - p) / 0.5


def _walk_book(levels: list[tuple[float, float]], size_needed: float) -> tuple[float | None, float]:
    """
    Walk book levels to simulate fill.
    levels: sorted list of (price, size) tuples
    Returns (avg_fill_price, filled_size)
    """
    filled = 0.0
    cost = 0.0
    for price, avail in levels:
        take = min(avail, size_needed - filled)
        cost += take * price
        filled += take
        if filled >= size_needed:
            break
    if filled == 0:
        return None, 0.0
    return cost / filled, filled


async def simulate_limit_buy(token_id: str, limit_price: float, size: float) -> dict:
    """
    Simulate a limit buy order. Fill if best ask <= limit_price with sufficient depth.
    Returns fill result dict.
    """
    book = ws_client.get_orderbook(token_id)
    asks = sorted(
        [(float(p), s) for p, s in book["asks"].items()],
        key=lambda x: x[0],
    )

    fillable = [(p, s) for p, s in asks if p <= limit_price + SLIPPAGE_BUFFER]
    if not fillable:
        return {"filled": False, "fill_price": None, "filled_size": 0.0}

    avg_price, filled_size = _walk_book(fillable, size)
    if filled_size < size:
        return {"filled": False, "fill_price": avg_price, "filled_size": filled_size, "partial": True}

    fees = filled_size * SLIPPAGE_BUFFER  # maker order: 0% taker fee
    return {
        "filled": True,
        "fill_price": avg_price,
        "filled_size": filled_size,
        "fees": fees,
    }


async def simulate_limit_sell(token_id: str, limit_price: float, size: float) -> dict:
    """Simulate a limit sell — fill if best bid >= limit_price."""
    book = ws_client.get_orderbook(token_id)
    bids = sorted(
        [(float(p), s) for p, s in book["bids"].items()],
        key=lambda x: -x[0],
    )

    fillable = [(p, s) for p, s in bids if p >= limit_price - SLIPPAGE_BUFFER]
    if not fillable:
        return {"filled": False, "fill_price": None, "filled_size": 0.0}

    avg_price, filled_size = _walk_book(fillable, size)
    if filled_size < size:
        return {"filled": False, "fill_price": avg_price, "filled_size": filled_size, "partial": True}

    fees = filled_size * SLIPPAGE_BUFFER  # maker order: 0% taker fee
    return {
        "filled": True,
        "fill_price": avg_price,
        "filled_size": filled_size,
        "fees": fees,
    }


async def simulate_market_sell(token_id: str, size: float) -> dict:
    """Simulate a market sell — walk bids immediately."""
    book = ws_client.get_orderbook(token_id)
    bids = sorted(
        [(float(p), s) for p, s in book["bids"].items()],
        key=lambda x: -x[0],
    )

    if not bids:
        return {"filled": False, "fill_price": None, "filled_size": 0.0}

    avg_price, filled_size = _walk_book(bids, size)
    fees = filled_size * avg_price * taker_fee_rate(avg_price or 0.5) + filled_size * SLIPPAGE_BUFFER
    return {
        "filled": filled_size >= size * 0.95,
        "fill_price": avg_price,
        "filled_size": filled_size,
        "fees": fees,
    }


def check_trigger(yes_token_id: str, no_token_id: str, threshold: float) -> tuple[str | None, float | None]:
    """
    Check if either side has hit the trigger threshold.
    Returns (winner_side, price) or (None, None).
    """
    yes_mid = ws_client.get_mid_price(yes_token_id)
    no_mid = ws_client.get_mid_price(no_token_id)

    if yes_mid and yes_mid >= threshold:
        return "YES", yes_mid
    if no_mid and no_mid >= threshold:
        return "NO", no_mid
    return None, None


async def poll_for_fill(
    token_id: str,
    limit_price: float,
    size: float,
    side: str,  # "buy" or "sell"
    timeout_seconds: float = 120.0,
    poll_interval: float = 1.0,
) -> dict:
    """Poll until fill or timeout — used during paper entry phase."""
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        if side == "buy":
            result = await simulate_limit_buy(token_id, limit_price, size)
        else:
            result = await simulate_limit_sell(token_id, limit_price, size)

        if result["filled"]:
            return result
        await asyncio.sleep(poll_interval)

    return {"filled": False, "fill_price": None, "filled_size": 0.0, "timed_out": True}
