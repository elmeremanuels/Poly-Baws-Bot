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


def get_coin_params(coin: str) -> dict:
    """Return exit config merged with per-coin overrides from coins.<coin>.

    Claude can set cross_threshold, initial_offset and ratchet_buffer per coin
    via _apply_claude_params; those values land in CONFIG["coins"][coin] and are
    picked up here so the peg-cross engine uses the per-coin tuned values.
    """
    from .config_loader import CONFIG
    base = dict(CONFIG["exit"])
    coin_cfg = CONFIG["coins"].get(coin, {})
    for k in ("cross_threshold", "initial_offset", "ratchet_buffer"):
        if k in coin_cfg:
            base[k] = coin_cfg[k]
    return base


def get_price_velocity(asset_id: str, window_secs: float = 5.0) -> float | None:
    """Price change rate in ¢/sec over the last window_secs seconds."""
    samples = _prices.get(asset_id)
    if not samples or len(samples) < 2:
        return None
    latest_ts, latest_price = samples[-1]
    cutoff = latest_ts - window_secs
    oldest = samples[0]
    for s in samples:
        if s[0] >= cutoff:
            oldest = s
            break
    time_delta = latest_ts - oldest[0]
    if time_delta <= 0:
        return None
    return (latest_price - oldest[1]) / time_delta
