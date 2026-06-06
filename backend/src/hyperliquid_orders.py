"""Hyperliquid perpetual futures order management.

Thin wrapper around hyperliquid-python-sdk. Paper mode: geen echte orders.
Live mode: vereist HYPERLIQUID_PRIVATE_KEY in .env.
"""
from __future__ import annotations

import os

from .config_loader import CONFIG
from .logger import log

_HL_URL = "https://api.hyperliquid.xyz"

# Module-level singletons (geïnitialiseerd bij eerste gebruik)
_info = None
_exchange = None
_wallet = None
_setup_done = False


def _setup() -> None:
    global _info, _exchange, _wallet, _setup_done
    if _setup_done:
        return
    _setup_done = True

    from hyperliquid.info import Info

    pk = (os.getenv("HYPERLIQUID_PRIVATE_KEY")
          or CONFIG.get("hyperliquid_trader", {}).get("private_key", ""))

    _info = Info(_HL_URL, skip_ws=True)

    if not pk or pk.startswith("${") or pk in ("0x...", ""):
        log.info("hl_setup_read_only", reason="geen private key — alleen lezen")
        return

    from eth_account import Account
    from hyperliquid.exchange import Exchange
    _wallet = Account.from_key(pk)
    _exchange = Exchange(_wallet, base_url=_HL_URL)
    log.info("hl_setup_live", address=_wallet.address[:12] + "…")


def get_price(coin: str) -> float | None:
    """Huidige mid-prijs via Hyperliquid info endpoint."""
    _setup()
    try:
        mids = _info.all_mids()
        v = mids.get(coin)
        return float(v) if v is not None else None
    except Exception as exc:
        log.warning("hl_price_error", coin=coin, error=str(exc)[:80])
        return None


def get_balance() -> float:
    """Account value in USDC (voor positiegroottes)."""
    _setup()
    if not (_exchange and _wallet):
        return 0.0
    try:
        state = _info.user_state(_wallet.address)
        return float(state.get("marginSummary", {}).get("accountValue", 0))
    except Exception as exc:
        log.warning("hl_balance_error", error=str(exc)[:80])
        return 0.0


def get_open_position(coin: str) -> dict | None:
    """Open positie voor coin, of None."""
    _setup()
    if not (_wallet and _info):
        return None
    try:
        state = _info.user_state(_wallet.address)
        for item in state.get("assetPositions", []):
            pos = item.get("position", {})
            if pos.get("coin") != coin:
                continue
            szi = float(pos.get("szi", 0))
            if abs(szi) < 1e-7:
                continue
            return {
                "coin": coin,
                "size": szi,          # positief = long, negatief = short
                "is_long": szi > 0,
                "entry_price": float(pos.get("entryPx") or 0),
                "unrealized_pnl": float(pos.get("unrealizedPnl") or 0),
            }
    except Exception as exc:
        log.warning("hl_position_error", coin=coin, error=str(exc)[:80])
    return None


def _parse_fill(resp: dict) -> tuple[float, float]:
    """Extraheer (avg_price, total_size) uit SDK response. (0,0) bij fout."""
    if not resp or resp.get("status") != "ok":
        return 0.0, 0.0
    statuses = (resp.get("response", {})
                    .get("data", {})
                    .get("statuses", []))
    for s in statuses:
        f = s.get("filled", {})
        avg_px  = float(f.get("avgPx", 0) or 0)
        total_sz = float(f.get("totalSz", 0) or 0)
        if total_sz > 0 and avg_px > 0:
            return avg_px, total_sz
    return 0.0, 0.0


def place_market_open(coin: str, is_long: bool, size_coin: float,
                      leverage: int = 5) -> tuple[float, float]:
    """Open market-positie. Retourneert (fill_price, filled_size) of (0,0)."""
    _setup()
    if not (_exchange and _wallet):
        log.warning("hl_open_no_exchange", coin=coin)
        return 0.0, 0.0
    try:
        _exchange.update_leverage(leverage, coin, is_cross=True)
        resp = _exchange.market_open(coin, is_buy=is_long, sz=round(size_coin, 6))
        price, size = _parse_fill(resp)
        if price > 0:
            log.info("hl_opened", coin=coin, is_long=is_long,
                     price=round(price, 2), size=size, leverage=leverage)
            return price, size
        log.error("hl_open_failed", coin=coin, resp=str(resp)[:200])
    except Exception as exc:
        log.error("hl_open_exception", coin=coin, error=str(exc)[:120])
    return 0.0, 0.0


def place_market_close(coin: str) -> tuple[float, float]:
    """Sluit volledige positie. Retourneert (fill_price, filled_size) of (0,0)."""
    _setup()
    if not (_exchange and _wallet):
        log.warning("hl_close_no_exchange", coin=coin)
        return 0.0, 0.0
    try:
        resp = _exchange.market_close(coin)
        price, size = _parse_fill(resp)
        if price > 0:
            log.info("hl_closed", coin=coin, price=round(price, 2), size=size)
            return price, size
        log.error("hl_close_failed", coin=coin, resp=str(resp)[:200])
    except Exception as exc:
        log.error("hl_close_exception", coin=coin, error=str(exc)[:120])
    return 0.0, 0.0
