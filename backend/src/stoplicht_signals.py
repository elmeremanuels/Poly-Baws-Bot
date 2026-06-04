"""Stoplicht signals for the Stoplicht Scalper mode.

Uses the same indicator_engine as the port-8502 stoplicht dashboard so both
show identical signals. The engine runs its own Kraken REST polling thread
(spot OFI, OBI, MOM, Perp OFI, CVD, S/R walls, regime).

Color mapping (threshold from config):
  GROEN  — score ≥ green_threshold  (default 0.60) AND direction not undecided
  ORANJE — score ≥ orange_threshold (default 0.35) AND direction not undecided
  ROOD   — otherwise

Direction:
  YES  — engine says "up"   (buy YES token, bet price goes UP)
  NO   — engine says "down" (buy NO token,  bet price goes DOWN)
"""
from __future__ import annotations

from .config_loader import CONFIG
from .indicator_engine import (
    Cache,
    get_cache,
    compute_direction,
    compile_levels,
    wall_message,
)
from .logger import log


def _cache(coin: str) -> Cache:
    return get_cache(coin)


def _thresholds() -> tuple[float, float]:
    cfg = CONFIG.get("stoplicht_scalper", {})
    return cfg.get("green_threshold", 0.60), cfg.get("orange_threshold", 0.35)


async def get_stoplicht(coin: str) -> tuple[str, str | None, float]:
    """Compute the stoplicht signal for a coin.

    Returns:
        (color: "GROEN"/"ORANJE"/"ROOD", direction: "YES"/"NO"/None, score: 0.0–1.0)
    """
    c = _cache(coin)
    direction_eng, score, signals = compute_direction(c)

    if direction_eng == "undecided" or score == 0.0:
        return "ROOD", None, 0.0

    direction = "YES" if direction_eng == "up" else "NO"
    green_thr, orange_thr = _thresholds()

    if score >= green_thr:
        color = "GROEN"
    elif score >= orange_thr:
        color = "ORANJE"
    else:
        color = "ROOD"

    log.debug(
        "stoplicht_computed",
        coin=coin, color=color, direction=direction, score=score,
        ofi=signals.get("spot_ofi"), obi=signals.get("obi"),
        mom=signals.get("mom"), cvd=signals.get("cvd"),
        regime=signals.get("regime"),
    )
    return color, direction, score


async def get_stoplicht_dict(coin: str, yes_token: str | None = None,
                              no_token: str | None = None) -> dict:
    """Extended stoplicht state dict for the scalper orchestrator.

    Returns:
        {
            "color": "GROEN"/"ORANJE"/"ROOD",
            "direction": "UP"/"DOWN"/None,
            "score": float,
            "consensus": bool,
            "confirmed": bool,
            "ofi": float|None,
            "obi": float|None,
            "mom": float|None,
            "cvd": float|None,
            "regime": str,
            "support_near": bool,
            "support_bounce_direction": "UP"/"DOWN"/None,
            "nearest_support": float|None,
            "nearest_resistance": float|None,
        }
    """
    c = _cache(coin)
    direction_eng, score, signals = compute_direction(c)

    if direction_eng == "undecided" or score == 0.0:
        yes_no_dir, color = None, "ROOD"
    else:
        yes_no_dir = "YES" if direction_eng == "up" else "NO"
        green_thr, orange_thr = _thresholds()
        color = "GROEN" if score >= green_thr else "ORANJE" if score >= orange_thr else "ROOD"
    ofi  = signals.get("spot_ofi")
    obi  = signals.get("obi")
    mom  = signals.get("mom")
    cvd  = signals.get("cvd")
    regime = signals.get("regime", "UNKNOWN")

    consensus = (color == "GROEN")

    # confirmed: Polymarket mid price moving in our direction
    confirmed = False
    if yes_no_dir and yes_token and no_token:
        from . import ws_client as _ws
        yes_mid = _ws.get_mid_price(yes_token)
        no_mid  = _ws.get_mid_price(no_token)
        if yes_mid and no_mid:
            if yes_no_dir == "YES" and yes_mid > 0.50:
                confirmed = True
            elif yes_no_dir == "NO" and no_mid > 0.50:
                confirmed = True
    elif yes_no_dir:
        confirmed = consensus

    # Wall-based support/resistance (identical to indicator_app logic)
    support_near = False
    support_bounce_direction = None
    nearest_support: float | None = None
    nearest_resistance: float | None = None

    price, supports, resistances = compile_levels(c)
    if price > 0:
        if supports:
            nearest_support = supports[0]["price"]
            sup_pct = (price - nearest_support) / price * 100
            if sup_pct < 0.35:
                support_near = True
                support_bounce_direction = "UP"

        if resistances:
            nearest_resistance = resistances[0]["price"]
            res_pct = (nearest_resistance - price) / price * 100
            if res_pct < 0.35:
                support_near = True
                support_bounce_direction = "DOWN"

    scalper_dir = "UP" if yes_no_dir == "YES" else ("DOWN" if yes_no_dir == "NO" else None)

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
        "regime": regime,
        "support_near": support_near,
        "support_bounce_direction": support_bounce_direction,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
    }


async def get_stoplicht_dashboard(coin: str) -> dict:
    """Stoplicht state dict for dashboard display."""
    color, yes_no_dir, score = await get_stoplicht(coin)
    c = _cache(coin)

    _, _, signals = compute_direction(c)
    price, supports, resistances = compile_levels(c)

    return {
        "color": color,
        "direction": yes_no_dir,
        "score": score,
        "ofi": signals.get("spot_ofi"),
        "obi": signals.get("obi"),
        "mom": signals.get("mom"),
        "cvd": signals.get("cvd"),
        "regime": signals.get("regime", "UNKNOWN"),
        "nearest_support":    supports[0]["price"]    if supports    else None,
        "nearest_resistance": resistances[0]["price"] if resistances else None,
    }
