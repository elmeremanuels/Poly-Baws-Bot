"""Multi-coin market scanner — discovers upcoming 5-min windows on Polymarket."""
import asyncio
import json
from datetime import datetime, timezone, timedelta
import httpx

from .config_loader import CONFIG
from .logger import log, write_event

GAMMA_URL = CONFIG["polymarket"]["gamma_url"]
COIN_FILTERS = {coin: cfg["market_filter"] for coin, cfg in CONFIG["coins"].items()}

_market_cache: dict[str, list[dict]] = {}
_last_refresh: dict[str, datetime] = {}


async def _fetch_markets_for_coin(coin: str, filter_slug: str) -> list[dict]:
    """Fetch upcoming events from Gamma API; extract nested markets."""
    now = datetime.now(timezone.utc)
    start_min = (now - timedelta(minutes=30)).isoformat()

    results = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            params = {
                "closed": "false",
                "limit": 500,
                "order": "startDate",
                "ascending": "true",
                "start_date_min": start_min,
            }
            resp = await client.get(f"{GAMMA_URL}/events", params=params)
            if resp.status_code == 200:
                events = resp.json()
                if not isinstance(events, list):
                    events = events.get("events", [])
                for event in events:
                    slug = (event.get("slug") or "").lower()
                    if filter_slug.lower() not in slug:
                        continue
                    for market in event.get("markets") or []:
                        normalized = _normalize_event_market(coin, event, market)
                        if normalized:
                            results.append(normalized)
    except Exception as e:
        log.error("scanner_fetch_error", coin=coin, error=str(e))
    return results


def _normalize_event_market(coin: str, event: dict, market: dict) -> dict | None:
    """Combine event metadata with nested market data."""
    clob_tokens = market.get("clobTokenIds")
    if isinstance(clob_tokens, str):
        try:
            clob_tokens = json.loads(clob_tokens)
        except json.JSONDecodeError:
            clob_tokens = None

    if not clob_tokens or len(clob_tokens) < 2:
        return None

    return {
        "coin": coin,
        "market_id": market.get("id") or market.get("conditionId"),
        "condition_id": market.get("conditionId"),
        "slug": event.get("slug", ""),
        "question": market.get("question") or event.get("title", ""),
        "yes_token": clob_tokens[0],
        "no_token": clob_tokens[1],
        "window_start": _parse_ts(event.get("startDate") or market.get("startDate")),
        "window_end": _parse_ts(event.get("endDate") or market.get("endDate")),
        "status": event.get("active", True),
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
    coins = [coin] if coin else [c for c in COIN_FILTERS if CONFIG["coins"][c]["enabled"]]
    await asyncio.gather(*[_refresh_coin(c) for c in coins])


async def _refresh_coin(coin: str) -> None:
    filter_slug = COIN_FILTERS[coin]
    markets = await _fetch_markets_for_coin(coin, filter_slug)
    now = datetime.now(timezone.utc)

    active = [
        m for m in markets
        if m["window_end"] and now - timedelta(minutes=1) < m["window_end"] < now + timedelta(hours=25)
    ]
    active.sort(key=lambda m: m["window_end"] or now)
    _market_cache[coin] = active
    _last_refresh[coin] = now
    log.info("scanner_refreshed", coin=coin, count=len(active))

    if active:
        first = active[0]
        log.info("scanner_first_market", coin=coin,
                 slug=first["slug"], question=first["question"][:80],
                 window_start=str(first["window_start"]),
                 window_end=str(first["window_end"]))
    else:
        log.warning("scanner_no_markets", coin=coin)
        await write_event(None, "scanner_alert", coin, {"reason": "no_markets"})


def get_upcoming_markets(coin: str, within_minutes: int = 30) -> list[dict]:
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=within_minutes)
    return [m for m in _market_cache.get(coin, [])
            if m["window_start"] and now <= m["window_start"] <= cutoff]


def get_next_windows(coin: str, n: int = 3) -> list[dict]:
    now = datetime.now(timezone.utc)
    return [m for m in _market_cache.get(coin, [])
            if m["window_end"] and m["window_end"] > now][:n]


def get_tradeable_market(coin: str) -> dict | None:
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
    while True:
        try:
            await refresh_markets()
        except Exception as e:
            log.error("scanner_loop_error", error=str(e))
        await asyncio.sleep(interval_seconds)
