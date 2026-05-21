"""Multi-coin market scanner — discovers upcoming 5-min windows on Polymarket."""
import asyncio
import json
from datetime import datetime, timezone, timedelta
from typing import Any
import httpx

from .config_loader import CONFIG
from .logger import log, write_event

GAMMA_URL = CONFIG["polymarket"]["gamma_url"]
COIN_FILTERS = {coin: cfg["market_filter"] for coin, cfg in CONFIG["coins"].items()}

# Cached markets: coin -> list of market dicts sorted by window_start asc
_market_cache: dict[str, list[dict]] = {}
_last_refresh: dict[str, datetime] = {}
_CACHE_TTL_SECONDS = 120


async def _fetch_markets_for_coin(coin: str, filter_slug: str) -> list[dict]:
    """Fetch upcoming markets from Gamma API filtered by slug fragment."""
    params = {
        "active": "true",
        "closed": "false",
        "limit": 50,
        "order": "startDate",
        "ascending": "true",
    }
    markets = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Search by tag/slug pattern
            resp = await client.get(
                f"{GAMMA_URL}/markets",
                params={**params, "tag": coin},
            )
            if resp.status_code == 200:
                data = resp.json()
                raw = data if isinstance(data, list) else data.get("markets", [])
                for m in raw:
                    slug = (m.get("slug") or "").lower()
                    q = (m.get("question") or "").lower()
                    if filter_slug.lower() in slug or filter_slug.lower() in q:
                        markets.append(_normalize_market(coin, m))
    except Exception as e:
        log.error("scanner_fetch_error", coin=coin, error=str(e))
    return markets


def _normalize_market(coin: str, raw: dict) -> dict:
    """Extract the fields we care about from a Gamma market object."""
    # Gamma returns token arrays for YES/NO token IDs
    tokens = raw.get("tokens") or raw.get("clobTokenIds") or []
    yes_token = None
    no_token = None
    if isinstance(tokens, list) and len(tokens) >= 2:
        yes_token = tokens[0]
        no_token = tokens[1]
    elif isinstance(tokens, dict):
        yes_token = tokens.get("yes") or tokens.get("YES")
        no_token = tokens.get("no") or tokens.get("NO")

    start_str = raw.get("startDate") or raw.get("start_date")
    end_str = raw.get("endDate") or raw.get("end_date")

    window_start = _parse_ts(start_str)
    window_end = _parse_ts(end_str)

    return {
        "coin": coin,
        "market_id": raw.get("id") or raw.get("conditionId"),
        "condition_id": raw.get("conditionId") or raw.get("condition_id"),
        "slug": raw.get("slug", ""),
        "question": raw.get("question", ""),
        "yes_token": yes_token,
        "no_token": no_token,
        "window_start": window_start,
        "window_end": window_end,
        "status": raw.get("active", True),
    }


def _parse_ts(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    try:
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return datetime.fromisoformat(ts_str).astimezone(timezone.utc)
    except Exception:
        return None


async def refresh_markets(coin: str | None = None) -> None:
    """Refresh market cache for one or all coins."""
    coins_to_refresh = [coin] if coin else list(COIN_FILTERS.keys())
    tasks = []
    for c in coins_to_refresh:
        if not CONFIG["coins"][c]["enabled"]:
            continue
        tasks.append(_refresh_coin(c))
    await asyncio.gather(*tasks)


async def _refresh_coin(coin: str) -> None:
    filter_slug = COIN_FILTERS[coin]
    markets = await _fetch_markets_for_coin(coin, filter_slug)
    now = datetime.now(timezone.utc)
    # Only keep future markets
    upcoming = [m for m in markets if m["window_start"] and m["window_start"] > now - timedelta(minutes=5)]
    upcoming.sort(key=lambda m: m["window_start"])
    _market_cache[coin] = upcoming
    _last_refresh[coin] = now
    log.info("scanner_refreshed", coin=coin, count=len(upcoming))
    await _healthcheck_coin(coin, filter_slug, upcoming)


async def _healthcheck_coin(coin: str, filter_slug: str, markets: list[dict]) -> None:
    """
    Verify that the returned markets actually match our slug pattern.
    Alerts (logs + persists event) if:
    - Zero markets found for an enabled coin
    - Any market slug doesn't contain the expected pattern (possible API structure change)
    """
    if not markets:
        msg = f"No upcoming markets found for {coin} (filter={filter_slug!r}). Gamma API may have changed slugs."
        log.warning("scanner_healthcheck_no_markets", coin=coin, filter=filter_slug, alert=True)
        await write_event(None, "scanner_alert", coin, {"reason": "no_markets", "filter": filter_slug})
        return

    mismatched = [
        m for m in markets
        if filter_slug.lower() not in (m.get("slug") or "").lower()
        and filter_slug.lower() not in (m.get("question") or "").lower()
    ]
    if mismatched:
        slugs = [m.get("slug", "") for m in mismatched[:3]]
        log.warning(
            "scanner_healthcheck_slug_mismatch",
            coin=coin,
            filter=filter_slug,
            mismatched_slugs=slugs,
            alert=True,
        )
        await write_event(
            None, "scanner_alert", coin,
            {"reason": "slug_mismatch", "filter": filter_slug, "sample_slugs": slugs},
        )


def get_upcoming_markets(coin: str, within_minutes: int = 30) -> list[dict]:
    """Return markets starting within `within_minutes` from now."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=within_minutes)
    markets = _market_cache.get(coin, [])
    return [m for m in markets if m["window_start"] and now <= m["window_start"] <= cutoff]


def get_next_windows(coin: str, n: int = 3) -> list[dict]:
    """Return the next n upcoming markets for a coin."""
    now = datetime.now(timezone.utc)
    markets = _market_cache.get(coin, [])
    future = [m for m in markets if m["window_start"] and m["window_start"] > now - timedelta(minutes=1)]
    return future[:n]


def get_tradeable_market(coin: str) -> dict | None:
    """Return the market that is within the entry window right now."""
    cfg = CONFIG["trading"]
    start_before = cfg["entry_start_minutes_before_window"]
    cutoff_before = cfg["entry_cutoff_minutes_before_window"]
    now = datetime.now(timezone.utc)

    for m in _market_cache.get(coin, []):
        ws = m["window_start"]
        if ws is None:
            continue
        minutes_to_start = (ws - now).total_seconds() / 60
        if cutoff_before <= minutes_to_start <= start_before:
            return m
    return None


async def scanner_loop(interval_seconds: int = 60) -> None:
    """Background loop that keeps the market cache fresh."""
    while True:
        try:
            await refresh_markets()
        except Exception as e:
            log.error("scanner_loop_error", error=str(e))
        await asyncio.sleep(interval_seconds)
