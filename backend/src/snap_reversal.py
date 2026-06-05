"""Snap reversal detector — hard crypto price moves over 8 hours.

A 'snap' is a rapid price move beyond a magnitude threshold.
When a fresh snap occurs AND a prior snap of the same direction exists
in the last 8 h, a hedge signal fires in the OPPOSITE direction (bet
on the reversal).

Psychology: clustered orders at round/mythical price levels
(BTC 60 000, 65 000, 70 000 …) cause rapid snap-backs when price
briefly breaks through.

Feed this detector from indicator_engine._poll_loop on every price
update (every POLL_SECS ≈ 3 s).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

SNAP_MIN_PCT     = 0.18    # min % move in SNAP_WINDOW_SECS to register a snap
SNAP_WINDOW_SECS = 60      # look-back window to measure the price change
SNAP_LOOKBACK_H  = 8       # how long snap events are remembered
SNAP_FRESH_SECS  = 90      # a snap must be < this old to trigger a hedge
ROUND_NEAR_PCT   = 0.40    # within 0.40% of a round level = "near round"
MIN_CONFIDENCE   = 0.35    # confidence floor to emit a hedge signal


@dataclass
class SnapEvent:
    ts:            float         # epoch seconds
    direction:     str           # "UP" or "DOWN"
    magnitude_pct: float         # absolute % move
    price_before:  float
    price_after:   float
    near_round:    float | None  # nearest round number if within ROUND_NEAR_PCT%


class SnapReversalDetector:
    """Per-coin detector. Call update() on every price tick; query hedge_signal() in the scalper."""

    def __init__(self) -> None:
        self._price_buf: list[tuple[float, float]] = []  # (ts, price)
        self._events:    list[SnapEvent]            = []
        self._lock = threading.Lock()

    def update(self, price: float) -> SnapEvent | None:
        """Feed a new price observation. Returns a SnapEvent if a snap was just detected."""
        if price <= 0:
            return None
        now       = time.time()
        cutoff_8h = now - SNAP_LOOKBACK_H * 3600

        with self._lock:
            self._price_buf.append((now, price))
            # Prune price buffer to 2× SNAP_WINDOW_SECS
            self._price_buf = [(t, p) for t, p in self._price_buf
                               if t > now - SNAP_WINDOW_SECS * 2]
            # Expire old snap events
            self._events = [e for e in self._events if e.ts > cutoff_8h]

            # Find reference price SNAP_WINDOW_SECS ago
            old = [(t, p) for t, p in self._price_buf if t <= now - SNAP_WINDOW_SECS]
            if not old:
                return None
            _, ref_price = old[-1]
            if ref_price <= 0:
                return None

            pct = (price - ref_price) / ref_price * 100
            if abs(pct) < SNAP_MIN_PCT:
                return None

            direction = "UP" if pct > 0 else "DOWN"
            # 30-second cooldown per direction to avoid duplicate events
            if self._events:
                last = self._events[-1]
                if now - last.ts < 30 and last.direction == direction:
                    return None

            near = _nearest_round(price)
            prox = abs(price - near) / price * 100
            event = SnapEvent(
                ts=now, direction=direction,
                magnitude_pct=round(abs(pct), 3),
                price_before=ref_price, price_after=price,
                near_round=near if prox < ROUND_NEAR_PCT else None,
            )
            self._events.append(event)
            return event

    def recent_events(self, hours: float = SNAP_LOOKBACK_H) -> list[SnapEvent]:
        """All snap events within the last `hours` hours."""
        cutoff = time.time() - hours * 3600
        with self._lock:
            return [e for e in self._events if e.ts > cutoff]

    def hedge_signal(self) -> dict | None:
        """Return a hedge dict if a fresh snap is confirmed by prior history.

        Returns None or::

          {
            "hedge_direction": "UP" | "DOWN",   # opposite of the snap → bet on reversal
            "snap_direction":  "UP" | "DOWN",
            "confidence":      float,
            "snap_magnitude":  float,
            "near_round":      float | None,
            "prior_count":     int,             # number of confirming prior snaps
          }
        """
        now = time.time()
        with self._lock:
            events = list(self._events)

        if not events:
            return None

        latest = events[-1]
        if now - latest.ts > SNAP_FRESH_SECS:
            return None

        prior = [e for e in events[:-1] if e.direction == latest.direction]
        if not prior:
            return None

        confidence = min(1.0, latest.magnitude_pct / 0.50)
        if confidence < MIN_CONFIDENCE:
            return None

        return {
            "hedge_direction": "DOWN" if latest.direction == "UP" else "UP",
            "snap_direction":  latest.direction,
            "confidence":      round(confidence, 3),
            "snap_magnitude":  latest.magnitude_pct,
            "near_round":      latest.near_round,
            "prior_count":     len(prior),
        }


def _nearest_round(price: float) -> float:
    """Nearest psychologically significant round level."""
    if price <= 0:
        return 0.0
    if price > 10_000:
        step = 1_000
    elif price > 1_000:
        step = 100
    elif price > 100:
        step = 10
    else:
        step = 1
    return float(round(price / step) * step)


# ── Global registry ────────────────────────────────────────────────────────────

_registry:      dict[str, SnapReversalDetector] = {}
_registry_lock = threading.Lock()


def get_detector(coin: str) -> SnapReversalDetector:
    with _registry_lock:
        if coin not in _registry:
            _registry[coin] = SnapReversalDetector()
        return _registry[coin]
