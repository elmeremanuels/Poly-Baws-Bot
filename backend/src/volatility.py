"""Realized volatility tracker — rolling window of mid-price samples per token."""
import math
import time
from collections import deque

_WINDOW_SECS = 60

_prices: dict[str, deque] = {}


def register_tokens(asset_ids: list[str]) -> None:
    for aid in asset_ids:
        if aid not in _prices:
            _prices[aid] = deque()


def on_price_update(asset_id: str, mid: float) -> None:
    now = time.monotonic()
    if asset_id not in _prices:
        _prices[asset_id] = deque()
    _prices[asset_id].append((now, mid))
    while _prices[asset_id] and now - _prices[asset_id][0][0] > _WINDOW_SECS:
        _prices[asset_id].popleft()


def get_realized_vol(asset_id: str) -> float | None:
    samples = _prices.get(asset_id)
    if not samples or len(samples) < 5:
        return None
    mids = [p for _, p in samples]
    returns = [math.log(mids[i] / mids[i - 1]) for i in range(1, len(mids)) if mids[i - 1] > 0]
    if len(returns) < 4:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / len(returns)
    return math.sqrt(var)
