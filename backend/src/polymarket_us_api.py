"""Polymarket US REST API client (api.polymarket.us).

Requires POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET_KEY environment variables,
generated at polymarket.us/developer.

All functions are no-ops (return None/[]) when credentials are not set,
so the bot runs normally without them.
"""
import os

from .logger import log

_KEY_ID_ENV = "POLYMARKET_US_KEY_ID"
_SECRET_ENV = "POLYMARKET_US_SECRET_KEY"


def _creds() -> tuple[str, str] | None:
    key_id = os.getenv(_KEY_ID_ENV)
    secret = os.getenv(_SECRET_ENV)
    if key_id and secret:
        return (key_id, secret)
    return None


async def get_account_balance() -> dict | None:
    """Return {current_balance, buying_power, asset_notional, open_orders} or None."""
    creds = _creds()
    if not creds:
        return None
    try:
        from polymarket_us import AsyncPolymarketUS
        async with AsyncPolymarketUS(key_id=creds[0], secret_key=creds[1], timeout=15) as client:
            resp = await client.account.balances()
        balances = resp.get("balances", [])
        if not balances:
            return None
        b = balances[0]
        return {
            "current_balance": b.get("currentBalance"),
            "buying_power": b.get("buyingPower"),
            "asset_notional": b.get("assetNotional"),
            "open_orders": b.get("openOrders"),
        }
    except Exception as e:
        log.warning("polymarket_us_balance_failed", error=str(e))
        return None


async def get_recent_activities(limit: int = 200) -> list[dict]:
    """Return recent TRADE and POSITION_RESOLUTION activities from the Polymarket US API."""
    creds = _creds()
    if not creds:
        return []
    try:
        from polymarket_us import AsyncPolymarketUS
        async with AsyncPolymarketUS(key_id=creds[0], secret_key=creds[1], timeout=15) as client:
            resp = await client.portfolio.activities({
                "limit": limit,
                "types": ["ACTIVITY_TYPE_TRADE", "ACTIVITY_TYPE_POSITION_RESOLUTION"],
            })
        return resp.get("activities", [])
    except Exception as e:
        log.warning("polymarket_us_activities_failed", error=str(e))
        return []


def compute_activities_pnl(activities: list[dict]) -> float:
    """Sum realizedPnl.value across all TRADE activities."""
    total = 0.0
    for act in activities:
        if act.get("type") != "ACTIVITY_TYPE_TRADE":
            continue
        trade = act.get("trade") or {}
        pnl = trade.get("realizedPnl") or {}
        try:
            total += float(pnl.get("value") or 0)
        except (TypeError, ValueError):
            pass
    return round(total, 4)


async def close_position(
    market_slug: str,
    current_price: float,
    slippage_bips: int = 500,
) -> dict | None:
    """Close the full open position for a market slug via Polymarket US API.

    Uses synchronous execution (blocks until fill, cancel, or expiry).
    slippage_bips: max acceptable slippage in basis points (500 = 5%).

    NOTE: This sells the ENTIRE remaining position for the market.
    Only call this when you want to exit all remaining shares (e.g., winner at force-exit).
    """
    creds = _creds()
    if not creds:
        raise ValueError(f"{_KEY_ID_ENV}/{_SECRET_ENV} not configured")
    from polymarket_us import AsyncPolymarketUS
    async with AsyncPolymarketUS(key_id=creds[0], secret_key=creds[1], timeout=60) as client:
        resp = await client.orders.close_position({
            "marketSlug": market_slug,
            "synchronousExecution": True,
            "maxBlockTime": "30s",
            "slippageTolerance": {
                "currentPrice": {"value": str(round(current_price, 4)), "currency": "USD"},
                "bips": slippage_bips,
            },
        })
    log.info("close_position_done", market_slug=market_slug, resp=resp)
    return resp
