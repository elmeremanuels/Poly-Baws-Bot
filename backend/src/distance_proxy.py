"""Afstand-tot-strike proxy via Polymarket mid-prijs (geen externe feed)."""
from __future__ import annotations
from . import ws_client


def get_winning_side(yes_token: str, no_token: str) -> dict | None:
    """Return de leidende kant en zijn mid-prijs (= afstand-proxy).

    Returns:
        {"direction": "UP"/"DOWN", "winning_token": str, "winning_mid": float}
        or None if prices unavailable.
    """
    yes_mid = ws_client.get_mid_price(yes_token)
    no_mid = ws_client.get_mid_price(no_token)
    if yes_mid is None or no_mid is None:
        return None
    if yes_mid >= no_mid:
        return {"direction": "UP", "winning_token": yes_token, "winning_mid": yes_mid}
    return {"direction": "DOWN", "winning_token": no_token, "winning_mid": no_mid}
