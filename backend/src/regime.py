"""Market regime detection and underlying asset price tracking.

Regimes per coin:
  TRENDING  — momentum continues after trigger; winners run far; trail patiently
  CHOPPY    — momentum reverses; high peg_cross rate; need faster / higher threshold
  RANGING   — underlying price oscillating in tight band; directional bias possible
  BREAKOUT  — fast explosive move; trigger fires quickly; volatility spike
  UNKNOWN   — insufficient data

The underlying asset price (real USD, e.g. SOL at $85.38) is tracked via a
rolling in-memory buffer. This is separate from the YES/NO token mid prices.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone

from .config_loader import CONFIG
from .logger import log

# ── Asset price buffer ────────────────────────────────────────────────────────

# {coin: deque of (unix_ts, price_usd)}
_asset_prices: dict[str, deque] = {}
_PRICE_WINDOW = 1800  # 30 minutes of history


def record_asset_price(coin: str, price: float) -> None:
    """Store a real-time asset price observation (called by price-feed module)."""
    buf = _asset_prices.setdefault(coin, deque())
    now = time.time()
    buf.append((now, price))
    # Prune observations older than 30 minutes
    cutoff = now - _PRICE_WINDOW
    while buf and buf[0][0] < cutoff:
        buf.popleft()


def get_asset_price_stats(coin: str, window_secs: float = 1800.0) -> dict | None:
    """Return price range stats for the last window_secs seconds.

    Returns:
        {low, high, current, range_abs, range_pct, price_position, n_samples}
        price_position: 0.0 = at low, 1.0 = at high (where in the range is price now)
    """
    buf = _asset_prices.get(coin)
    if not buf:
        return None
    now = time.time()
    cutoff = now - window_secs
    samples = [(ts, p) for ts, p in buf if ts >= cutoff]
    if len(samples) < 5:
        return None
    prices = [p for _, p in samples]
    low = min(prices)
    high = max(prices)
    current = prices[-1]
    range_abs = high - low
    if range_abs < 1e-8:
        return None
    return {
        "low": round(low, 4),
        "high": round(high, 4),
        "current": round(current, 4),
        "range_abs": round(range_abs, 4),
        "range_pct": round(range_abs / current * 100, 3),
        "price_position": round((current - low) / range_abs, 3),  # 0=bottom, 1=top
        "n_samples": len(samples),
        "window_secs": window_secs,
    }


# ── Regime detection ──────────────────────────────────────────────────────────

_REGIME_HISTORY: dict[str, deque] = {}  # {coin: deque of (ts, regime)}
_current_regime: dict[str, str] = {}


def detect_regime(coin: str, recent_trades: list[dict]) -> str:
    """Classify current market regime for a coin from recent trade outcomes.

    Uses completed triggered trades (last 10 max) to derive:
    - peg_cross_rate    → fraction of trades that exited via peg_cross (CHOPPY signal)
    - avg_winner_travel → how far winners moved after trigger (TRENDING signal)
    - avg_trigger_speed → seconds from window start to trigger (BREAKOUT signal)

    Falls back to asset price range analysis when trade history is thin.
    """
    triggered = [t for t in recent_trades if t.get("trigger_hit")][-10:]
    n = len(triggered)

    # ── Asset price range check (always available if price feed is running) ───
    price_stats = get_asset_price_stats(coin, window_secs=1800)
    if price_stats and price_stats["range_pct"] < 0.4 and price_stats["n_samples"] >= 20:
        regime = "RANGING"
        _store_regime(coin, regime)
        return regime

    if n < 3:
        return _current_regime.get(coin, "UNKNOWN")

    peg_cross_count = sum(
        1 for t in triggered
        if (t.get("winner_exit_reason") or "") == "peg_cross"
    )
    peg_cross_rate = peg_cross_count / n

    winner_travels = []
    for t in triggered:
        wp = t.get("winner_exit_price")
        tt = t.get("trigger_threshold") or CONFIG["trading"]["trigger_threshold"]
        if wp is not None:
            winner_travels.append(abs(wp - tt))
    avg_travel = sum(winner_travels) / len(winner_travels) if winner_travels else 0.0

    speeds = [t["time_since_window_start"] for t in triggered
              if t.get("time_since_window_start") is not None]
    avg_speed = sum(speeds) / len(speeds) if speeds else 150.0

    # ── Classification rules ──────────────────────────────────────────────────
    if peg_cross_rate >= 0.45 and avg_travel < 0.07:
        regime = "CHOPPY"
    elif avg_speed < 60 and avg_travel > 0.10:
        regime = "BREAKOUT"
    elif peg_cross_rate <= 0.25 and avg_travel > 0.08:
        regime = "TRENDING"
    else:
        regime = "NORMAL"

    _store_regime(coin, regime)
    return regime


def _store_regime(coin: str, regime: str) -> None:
    _current_regime[coin] = regime
    buf = _REGIME_HISTORY.setdefault(coin, deque(maxlen=200))
    buf.append((time.time(), regime))


def get_current_regime(coin: str) -> str:
    return _current_regime.get(coin, "UNKNOWN")


def get_directional_bias(coin: str) -> str | None:
    """Return directional bias for entry based on regime + price position.

    RANGING regime only:
      price_position >= 0.75 (near top of 30-min range) → bias DOWN (buy NO heavier)
      price_position <= 0.25 (near bottom of range)      → bias UP  (buy YES heavier)
      Otherwise → no bias (buy 50/50 as normal)

    Returns "UP", "DOWN", or None.
    """
    if get_current_regime(coin) != "RANGING":
        return None
    stats = get_asset_price_stats(coin, window_secs=1800)
    if not stats:
        return None
    pos = stats["price_position"]
    if pos >= 0.75:
        return "DOWN"
    if pos <= 0.25:
        return "UP"
    return None


def get_regime_stats() -> dict:
    """Return current regime + price stats for all coins (for dashboard / Claude)."""
    result = {}
    for coin in CONFIG.get("coins", {}):
        result[coin] = {
            "regime": get_current_regime(coin),
            "bias": get_directional_bias(coin),
            "price_stats": get_asset_price_stats(coin),
        }
    return result


# ── Per-regime parameter profiles ─────────────────────────────────────────────

# In-memory learned overrides: {regime: {cross_threshold, initial_offset, ...}}
# Populated by the learning orchestrator after each analysis cycle.
_learned_profiles: dict[str, dict] = {}


def get_regime_params(regime: str) -> dict:
    """Return parameter overrides for a given regime.

    Layers: config.yaml regime_profiles (baseline) ← _learned_profiles (learned).
    Only returns keys present in the profile (caller decides which to apply).
    """
    base = dict(CONFIG.get("regime_profiles", {}).get(regime, {}))
    learned = _learned_profiles.get(regime, {})
    base.update(learned)
    return base


def update_regime_profile(regime: str, params: dict) -> None:
    """Store learned exit params for a regime (called after a learning cycle).

    Only keys relevant to exit behavior are kept; reasoning/scores are ignored.
    """
    _PROFILE_KEYS = ("cross_threshold", "initial_offset", "ratchet_buffer", "trigger_threshold_delta")
    extracted: dict = {}
    # Accept both flat dict and nested coin_params structure
    for k in _PROFILE_KEYS:
        if k in params:
            extracted[k] = float(params[k])
    # If nested under global_params
    gp = params.get("global_params") or {}
    if not extracted:
        # Try to pull cross_threshold from first coin_params entry as representative
        cp_values = list((params.get("coin_params") or {}).values())
        if cp_values:
            for k in ("cross_threshold", "initial_offset", "ratchet_buffer"):
                vals = [cp[k] for cp in cp_values if k in cp]
                if vals:
                    extracted[k] = round(sum(vals) / len(vals), 4)
    if extracted:
        _learned_profiles[regime] = extracted
        log.info("regime_profile_updated", regime=regime, params=extracted)


def get_bias_certainty(coin: str) -> tuple[str | None, float]:
    """Certainty score for the directional bias signal (0.0–1.0).

    0.0 = price just at threshold (0.75 or 0.25 of 30-min range).
    1.0 = price at extreme (1.0 = top or 0.0 = bottom of range).
    Only non-zero in RANGING regime.
    """
    if get_current_regime(coin) != "RANGING":
        return None, 0.0
    stats = get_asset_price_stats(coin)
    if not stats:
        return None, 0.0
    pos = stats["price_position"]
    if pos >= 0.75:
        return "DOWN", min(1.0, (pos - 0.75) / 0.25)
    if pos <= 0.25:
        return "UP", min(1.0, (0.25 - pos) / 0.25)
    return None, 0.0


def get_effective_trigger_threshold(coin: str) -> float:
    """Coin trigger threshold with regime delta applied.

    Used by both triggers.py and monitor.py so the detection threshold is
    consistent between entry-guard and live trigger-fire.
    """
    base = CONFIG["coins"].get(coin, {}).get(
        "trigger_threshold", CONFIG["trading"]["trigger_threshold"]
    )
    regime = get_current_regime(coin)
    delta = get_regime_params(regime).get("trigger_threshold_delta", 0.0)
    return max(0.65, min(0.85, base + delta))
