"""Stoplicht signals for the Stoplicht Scalper mode.

Five-signal directional indicator:
  OFI  — Order Flow Imbalance (spot Kraken trades, 35%)
  OBI  — Order Book Imbalance (top-25 bid/ask depth, 28%)
  MOM  — VWAP momentum (regime._asset_prices deque, 45s window, 20%)
  Perp — Perpetual OFI (Kraken futures, 12%)
  CVD  — Cumulative Volume Delta slope (acceleration, 5%)

Signal colors:
  GROEN  — combined score ≥ green_threshold (default 0.60)
  ORANJE — combined score ≥ orange_threshold (default 0.35)
  ROOD   — combined score < orange_threshold or no direction

Support/Resistance wall detection uses 25-level order book depth,
identical to the indicator_app (port 8502).
"""
from __future__ import annotations

import time

import httpx

from .config_loader import CONFIG
from .logger import log

_KRAKEN_REST = "https://api.kraken.com/0/public"

_COIN_TO_KRAKEN: dict[str, str] = {
    "BTC":  "XBTUSD",
    "ETH":  "ETHUSD",
    "SOL":  "SOLUSD",
    "XRP":  "XRPUSD",
    "DOGE": "XDGEUSD",
}

# Kraken /Depth cache: {coin: (unix_ts, book_dict)}
_depth_cache: dict[str, tuple[float, dict]] = {}
_DEPTH_CACHE_TTL = 2.0


async def _fetch_kraken_depth(coin: str) -> dict | None:
    """Fetch top-25 bid/ask levels from Kraken /Depth. Cached for 2 seconds."""
    cached = _depth_cache.get(coin)
    if cached and time.time() - cached[0] < _DEPTH_CACHE_TTL:
        return cached[1]

    pair = _COIN_TO_KRAKEN.get(coin)
    if not pair:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{_KRAKEN_REST}/Depth",
                params={"pair": pair, "count": 25},
            )
        if r.status_code != 200:
            return None
        data = r.json()
        result = data.get("result", {})
        book = result.get(pair) or next(iter(result.values()), None)
        if not book:
            return None
        _depth_cache[coin] = (time.time(), book)
        return book
    except Exception as exc:
        log.debug("stoplicht_depth_error", coin=coin, error=str(exc))
        return None


def _compute_obi(book: dict) -> float | None:
    """Order Book Imbalance = bid_volume / (bid_vol + ask_vol) for top-10 levels.

    0.5 = neutral; > 0.5 = bid-heavy (bullish); < 0.5 = ask-heavy (bearish).
    """
    try:
        bids = book.get("bids", [])[:10]
        asks = book.get("asks", [])[:10]
        bid_vol = sum(float(lv[1]) for lv in bids)
        ask_vol = sum(float(lv[1]) for lv in asks)
        total = bid_vol + ask_vol
        if total < 1e-8:
            return None
        return bid_vol / total
    except Exception:
        return None


def _compute_book_walls(book: dict, wall_threshold: float = 1.8) -> tuple[list, list, float]:
    """Detect large bid/ask clusters within 1.5% of current price.

    Returns (supports, resistances, current_price).
    Each entry: {"price": float, "vol": float}
    Supports = bid walls below current price.
    Resistances = ask walls above current price.
    Mirrors indicator_app._book_walls() logic.
    """
    try:
        bids = {float(p): float(v) for p, v, *_ in book.get("bids", [])[:25]}
        asks = {float(p): float(v) for p, v, *_ in book.get("asks", [])[:25]}

        if not bids or not asks:
            return [], [], 0.0

        best_bid = max(bids.keys())
        best_ask = min(asks.keys())
        current_price = (best_bid + best_ask) / 2

        sup_bids = {p: v for p, v in bids.items()
                    if current_price * 0.985 <= p < current_price}
        res_asks = {p: v for p, v in asks.items()
                    if current_price < p <= current_price * 1.015}

        def walls(levels: dict, desc: bool) -> list:
            if not levels:
                return []
            avg = sum(levels.values()) / len(levels)
            out = [{"price": p, "vol": v}
                   for p, v in levels.items() if v >= avg * wall_threshold]
            return sorted(out, key=lambda x: x["price"], reverse=desc)

        return walls(sup_bids, True), walls(res_asks, False), current_price
    except Exception:
        return [], [], 0.0


def _compute_momentum(coin: str, window_secs: float = 45.0) -> float | None:
    """VWAP momentum from regime._asset_prices.

    Splits window in halves: recent VWAP vs earlier VWAP.
    Returns -1..+1 where 0.05% drift maps to ±1.0.
    """
    from . import regime as _regime

    buf = _regime._asset_prices.get(coin)
    if not buf:
        return None
    now = time.time()
    cutoff = now - window_secs
    mid = now - window_secs / 2

    recent = [(p, 1.0) for ts, p in buf if ts >= mid]
    early = [(p, 1.0) for ts, p in buf if cutoff <= ts < mid]

    if len(recent) < 3 or len(early) < 3:
        return None

    def vwap(lst: list) -> float:
        v = sum(q for _, q in lst)
        return sum(p * q for p, q in lst) / v if v > 1e-10 else 0.0

    r, e = vwap(recent), vwap(early)
    if e == 0:
        return None
    return max(-1.0, min(1.0, (r - e) / e * 100 / 0.05))


def _compute_cvd_slope(coin: str, window_secs: float = 60.0) -> float | None:
    """Cumulative Volume Delta slope — measures acceleration of net buy/sell pressure.

    Splits window in halves: compares early vs late net signed volume.
    Positive = buying accelerating; negative = selling accelerating.
    Normalized by total volume: returns -1..+1.
    Mirrors indicator_app._cvd_slope() logic using signals._trades buffer.
    """
    from . import signals as _sig

    buf = _sig._trades.get(coin)
    if not buf:
        return None

    now = time.time()
    cutoff = now - window_secs
    mid = cutoff + window_secs / 2

    early = sum((q if b else -q) for ts, q, b in buf if cutoff <= ts < mid)
    late = sum((q if b else -q) for ts, q, b in buf if ts >= mid)
    total = sum(abs(q) for ts, q, _ in buf if ts >= cutoff)

    if total < 1e-8:
        return None
    return max(-1.0, min(1.0, (late - early) / total))


async def get_stoplicht(coin: str) -> tuple[str, str | None, float]:
    """Compute the stoplicht signal for a coin.

    Returns:
        (color: "GROEN"/"ORANJE"/"ROOD", direction: "YES"/"NO"/None, score: 0.0–1.0)
        "YES" = bullish (buy YES token, bet price goes UP)
        "NO"  = bearish (buy NO token, bet price goes DOWN)
    """
    from . import signals as _sig

    cfg = CONFIG.get("stoplicht_scalper", {})
    green_thr = cfg.get("green_threshold", 0.60)
    orange_thr = cfg.get("orange_threshold", 0.35)

    ofi = _sig.get_order_flow_imbalance(coin, window_secs=60.0)
    perp_ofi = _sig.get_perp_order_flow_imbalance(coin, window_secs=60.0)
    mom = _compute_momentum(coin, window_secs=45.0)
    cvd = _compute_cvd_slope(coin, window_secs=60.0)
    book = await _fetch_kraken_depth(coin)
    obi = _compute_obi(book) if book else None

    bull = 0.0
    bear = 0.0

    # OFI spot — primary gate (neutral zone 0.45–0.55 contributes nothing)
    if ofi is not None:
        if ofi > 0.55:
            bull += min(0.35, (ofi - 0.55) / 0.45 * 0.35)
        elif ofi < 0.45:
            bear += min(0.35, (0.45 - ofi) / 0.45 * 0.35)

    # OBI — strongest sub-minute predictor (Cont et al. 2014)
    if obi is not None:
        if obi > 0.55:
            bull += min(0.28, (obi - 0.55) / 0.45 * 0.28)
        elif obi < 0.45:
            bear += min(0.28, (0.45 - obi) / 0.45 * 0.28)

    # VWAP momentum
    if mom is not None:
        if mom > 0.1:
            bull += min(0.20, mom * 0.20)
        elif mom < -0.1:
            bear += min(0.20, abs(mom) * 0.20)

    # Perpetual OFI — confirmation
    if perp_ofi is not None:
        if perp_ofi > 0.55:
            bull += min(0.12, (perp_ofi - 0.55) / 0.45 * 0.12)
        elif perp_ofi < 0.45:
            bear += min(0.12, (0.45 - perp_ofi) / 0.45 * 0.12)

    # CVD slope — acceleration confirmation (5%)
    if cvd is not None and abs(cvd) > 0.05:
        if cvd > 0:
            bull += min(0.05, abs(cvd) * 0.05)
        else:
            bear += min(0.05, abs(cvd) * 0.05)

    score = max(bull, bear)

    if score < 0.01:
        return "ROOD", None, 0.0

    direction = "YES" if bull >= bear else "NO"

    if score >= green_thr:
        color = "GROEN"
    elif score >= orange_thr:
        color = "ORANJE"
    else:
        color = "ROOD"

    log.debug(
        "stoplicht_computed",
        coin=coin, color=color, direction=direction, score=round(score, 3),
        ofi=ofi, obi=round(obi, 3) if obi is not None else None,
        mom=round(mom, 3) if mom is not None else None,
        cvd=round(cvd, 3) if cvd is not None else None,
    )
    return color, direction, round(score, 3)


async def get_stoplicht_dict(coin: str, yes_token: str | None = None, no_token: str | None = None) -> dict:
    """Extended stoplicht state dict for the scalper orchestrator.

    Returns:
        {
            "color": "GROEN"/"ORANJE"/"ROOD",
            "direction": "UP"/"DOWN"/None,
            "score": float,
            "consensus": bool,            # True = GROEN (all indicators aligned)
            "confirmed": bool,            # market price moving in our direction
            "ofi": float|None,
            "obi": float|None,
            "mom": float|None,
            "cvd": float|None,
            "support_near": bool,         # True = book wall within 0.35% of price
            "support_bounce_direction": "UP"/"DOWN"/None,
            "nearest_support": float|None,  # price of nearest bid wall
            "nearest_resistance": float|None, # price of nearest ask wall
        }
    """
    from . import signals as _sig

    color, direction, score = await get_stoplicht(coin)

    ofi = _sig.get_order_flow_imbalance(coin, window_secs=60.0)
    mom = _compute_momentum(coin, window_secs=45.0)
    cvd = _compute_cvd_slope(coin, window_secs=60.0)
    book = await _fetch_kraken_depth(coin)
    obi = _compute_obi(book) if book else None

    consensus = (color == "GROEN")

    # confirmed: Polymarket mid price moving in our direction
    confirmed = False
    if direction and yes_token and no_token:
        from . import ws_client as _ws
        yes_mid = _ws.get_mid_price(yes_token)
        no_mid = _ws.get_mid_price(no_token)
        if yes_mid and no_mid:
            if direction == "YES" and yes_mid > 0.50:
                confirmed = True
            elif direction == "NO" and no_mid > 0.50:
                confirmed = True
    elif direction:
        confirmed = consensus

    # Wall-based support/resistance detection (mirrors indicator_app logic)
    support_near = False
    support_bounce_direction = None
    nearest_support: float | None = None
    nearest_resistance: float | None = None

    if book:
        supports, resistances, current_price = _compute_book_walls(book)
        if current_price > 0:
            if supports:
                nearest_support = supports[0]["price"]
                sup_pct = (current_price - nearest_support) / current_price * 100
                if sup_pct < 0.35:
                    support_near = True
                    support_bounce_direction = "UP"

            if resistances:
                nearest_resistance = resistances[0]["price"]
                res_pct = (nearest_resistance - current_price) / current_price * 100
                if res_pct < 0.35:
                    support_near = True
                    support_bounce_direction = "DOWN"

    scalper_dir = "UP" if direction == "YES" else ("DOWN" if direction == "NO" else None)

    return {
        "color": color,
        "direction": scalper_dir,
        "score": score,
        "consensus": consensus,
        "confirmed": confirmed,
        "ofi": ofi,
        "obi": obi,
        "mom": mom,
        "cvd": cvd,
        "support_near": support_near,
        "support_bounce_direction": support_bounce_direction,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
    }


async def get_stoplicht_dashboard(coin: str) -> dict:
    """Stoplicht state dict for dashboard display."""
    from . import signals as _sig
    from . import regime as _regime

    color, direction, score = await get_stoplicht(coin)
    ofi = _sig.get_order_flow_imbalance(coin, window_secs=60.0)
    mom = _compute_momentum(coin, window_secs=45.0)
    cvd = _compute_cvd_slope(coin, window_secs=60.0)
    book = await _fetch_kraken_depth(coin)
    obi = _compute_obi(book) if book else None

    supports, resistances, current_price = (
        _compute_book_walls(book) if book else ([], [], 0.0)
    )

    return {
        "color": color,
        "direction": direction,
        "score": score,
        "ofi": ofi,
        "obi": obi,
        "mom": mom,
        "cvd": cvd,
        "regime": _regime.get_current_regime(coin),
        "nearest_support": supports[0]["price"] if supports else None,
        "nearest_resistance": resistances[0]["price"] if resistances else None,
    }
