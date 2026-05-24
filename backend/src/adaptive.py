"""Real-time adaptive threshold tuner: adjusts per-coin trigger_threshold during deploy phase.

After each triggered, closed trade the EMA of net_pnl/share is updated.
When both the EMA and the rolling-window average fall below LOSS_THRESHOLD the
trigger_threshold is nudged up (fewer but stronger signals); when both exceed
PROFIT_THRESHOLD the threshold is nudged back down to capture more volume.

Changes are small and capped at [0.65, 0.85] to stay within CLOB safety bounds.
State is in-memory only — a bot restart resets it cleanly.
"""
from collections import deque

from .config_loader import CONFIG
from .logger import log

_WINDOW = 5
_ALPHA = 0.35           # EMA factor — higher = reacts faster to recent trades
_RAISE_STEP = 0.01      # raise threshold when losing consistently
_LOWER_STEP = 0.005     # lower threshold when profitable consistently
_LOSS_THRESHOLD = -0.03     # net_pnl/share below which we raise the bar
_PROFIT_THRESHOLD = 0.05    # net_pnl/share above which we can lower it
_MIN_TRADES = 3         # minimum trades in window before any adjustment

_ema: dict[str, float] = {}
_window: dict[str, deque] = {}
_adjustments: dict[str, int] = {}


def record_closed_trade(
    coin: str,
    net_pnl: float,
    size: float,
    loser_exit_price: float,
    break_even_price: float,
) -> None:
    """Update EMA and adjust trigger_threshold for coin if consistently off-target."""
    if size <= 0:
        return
    pnl_per_share = net_pnl / size

    prev = _ema.get(coin)
    _ema[coin] = _ALPHA * pnl_per_share + (1 - _ALPHA) * prev if prev is not None else pnl_per_share

    q = _window.setdefault(coin, deque(maxlen=_WINDOW))
    q.append(pnl_per_share)

    if len(q) < _MIN_TRADES:
        return

    avg = sum(q) / len(q)
    ema_val = _ema[coin]
    current = _get_threshold(coin)
    action = None
    new_threshold = current

    if avg < _LOSS_THRESHOLD and ema_val < _LOSS_THRESHOLD:
        new_threshold = min(0.85, round(current + _RAISE_STEP, 3))
        action = "raised"
    elif avg > _PROFIT_THRESHOLD and ema_val > _PROFIT_THRESHOLD and len(q) >= _WINDOW:
        new_threshold = max(0.65, round(current - _LOWER_STEP, 3))
        action = "lowered"

    if action and new_threshold != current:
        if coin not in CONFIG["coins"]:
            CONFIG["coins"][coin] = {}
        CONFIG["coins"][coin]["trigger_threshold"] = new_threshold
        _adjustments[coin] = _adjustments.get(coin, 0) + 1
        log.info(
            "adaptive_threshold_adjusted",
            coin=coin,
            action=action,
            old=current,
            new=new_threshold,
            avg_pnl_per_share=round(avg, 4),
            ema_pnl_per_share=round(ema_val, 4),
            loser_exit=round(loser_exit_price, 4),
            break_even=round(break_even_price, 4),
            total_adjustments=_adjustments[coin],
        )


def _get_threshold(coin: str) -> float:
    return CONFIG["coins"].get(coin, {}).get(
        "trigger_threshold", CONFIG["trading"]["trigger_threshold"]
    )


def get_stats() -> dict:
    """Return current adaptive state — included in Claude's analysis prompt."""
    result = {}
    for coin in CONFIG["coins"]:
        q = _window.get(coin)
        result[coin] = {
            "current_threshold": _get_threshold(coin),
            "ema_pnl_per_share": round(_ema.get(coin, 0.0), 4),
            "recent_avg_pnl_per_share": round(sum(q) / len(q), 4) if q else None,
            "trades_in_window": len(q) if q else 0,
            "adjustments_made": _adjustments.get(coin, 0),
        }
    return result
