"""Multi-coin market scanner — discovers upcoming N-min windows on Polymarket."""
import asyncio
import json
import re
from datetime import datetime, timezone, timedelta
import httpx

from .config_loader import CONFIG
from .logger import log, write_event, save_dashboard_state
from . import volatility

GAMMA_URL = CONFIG["polymarket"]["gamma_url"]

# COIN_FILTERS wordt dynamisch berekend in refresh_markets zodat runtime-wijzigingen
# in config.yaml (via het dashboard) direct worden opgepikt zonder herstart.
# De module-level dict blijft beschikbaar voor externe callers (scanner_test etc.)
COIN_FILTERS = {coin: cfg["market_filter"] for coin, cfg in CONFIG["coins"].items()}

_market_cache: dict[str, list[dict]] = {}
_last_refresh: dict[str, datetime] = {}

# Shared events cache so all 5 coins share one API call per refresh cycle
_events_cache: list[dict] = []
_events_cache_ts: datetime | None = None
_EVENTS_CACHE_TTL = timedelta(seconds=45)


async def _fetch_all_events() -> list[dict]:
    """Fetch upcoming events from Gamma API (one call shared by all coins).

    Uses end_date_max to limit to the next 26 hours and a high limit (2000) to
    ensure all 5 coins' 5-minute windows (≤1560 markets) fit in a single response.
    Without end_date_max the limit-500 default silently drops coins near the tail.
    """
    global _events_cache, _events_cache_ts
    now = datetime.now(timezone.utc)
    if _events_cache_ts and (now - _events_cache_ts) < _EVENTS_CACHE_TTL:
        return _events_cache

    end_max = (now + timedelta(hours=26)).isoformat()
    params = {
        "closed": "false",
        "limit": 2000,
        "order": "endDate",
        "ascending": "true",
        "end_date_min": now.isoformat(),
        "end_date_max": end_max,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{GAMMA_URL}/events", params=params)
        if resp.status_code == 200:
            events = resp.json()
            if not isinstance(events, list):
                events = events.get("events", [])
            _events_cache = events
            _events_cache_ts = now
            log.debug("gamma_events_fetched", count=len(events))
        else:
            log.warning("gamma_events_bad_status", status=resp.status_code)
    except Exception as e:
        log.error("gamma_events_fetch_error", error=str(e))

    return _events_cache


async def _fetch_markets_for_coin(coin: str, filter_slug: str) -> list[dict]:
    """Filter the shared events cache for this coin's updown markets."""
    events = await _fetch_all_events()
    results = []
    for event in events:
        slug = (event.get("slug") or "").lower()
        if filter_slug.lower() not in slug:
            continue
        for market in event.get("markets") or []:
            normalized = _normalize_event_market(coin, event, market)
            if normalized:
                results.append(normalized)
    if not results:
        log.warning("scanner_coin_no_events", coin=coin, filter=filter_slug,
                    total_events=len(events))
    return results


def _normalize_event_market(coin: str, event: dict, market: dict) -> dict | None:
    """Combine event metadata with nested market data. Window start is parsed from the slug
    epoch suffix because Gamma's startDate field is the event creation time (~23h before
    the actual 5M window opens), not the window opening time."""
    clob_tokens = market.get("clobTokenIds")
    if isinstance(clob_tokens, str):
        try:
            clob_tokens = json.loads(clob_tokens)
        except json.JSONDecodeError:
            clob_tokens = None

    if not clob_tokens or len(clob_tokens) < 2:
        return None

    slug = event.get("slug", "")

    # Parse window_start and duration from slug.
    # Pattern: {coin}-updown-{N}m-{unix_epoch}  e.g. btc-updown-15m-1234567890
    window_start = None
    window_end = None
    parts = slug.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        try:
            window_start = datetime.fromtimestamp(int(parts[1]), tz=timezone.utc)
            # Extract duration: look for "{N}m" suffix in the prefix part
            dur_match = re.search(r"-(\d+)m$", parts[0])
            duration_mins = int(dur_match.group(1)) if dur_match else 5
            window_end = window_start + timedelta(minutes=duration_mins)
        except (ValueError, OSError):
            pass

    # Fallback to event.endDate if slug parsing failed
    if window_end is None:
        window_end = _parse_ts(event.get("endDate"))
        if window_end:
            window_start = window_end - timedelta(minutes=5)

    return {
        "coin": coin,
        "market_id": market.get("id") or market.get("conditionId"),
        "condition_id": market.get("conditionId"),
        "slug": slug,
        "question": market.get("question") or event.get("title", ""),
        "yes_token": clob_tokens[0],
        "no_token": clob_tokens[1],
        "window_start": window_start,
        "window_end": window_end,
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
    if coin:
        coins = [coin]
    else:
        # Lees direct uit CONFIG zodat wijzigingen via het dashboard direct werken,
        # ongeacht de module-load-time snapshot in COIN_FILTERS.
        coins = [
            c for c, cfg in CONFIG["coins"].items()
            if cfg.get("enabled") and cfg.get("market_filter")
        ]
    log.debug("scanner_refresh_coins", coins=coins)
    # Sequential — parallel gather caused 5 simultaneous DB writes → database is locked
    for c in coins:
        await _refresh_coin(c)


async def _refresh_coin(coin: str) -> None:
    # Lees market_filter direct uit CONFIG (niet uit COIN_FILTERS snapshot)
    filter_slug = CONFIG["coins"].get(coin, {}).get("market_filter") or COIN_FILTERS.get(coin, "")
    markets = await _fetch_markets_for_coin(coin, filter_slug)
    now = datetime.now(timezone.utc)

    active = [
        m for m in markets
        if m["window_end"] and now - timedelta(minutes=1) < m["window_end"] < now + timedelta(hours=25)
    ]
    active.sort(key=lambda m: m["window_end"] or now)
    _market_cache[coin] = active
    _last_refresh[coin] = now

    tokens = []
    for m in active:
        if m.get("yes_token"):
            tokens.append(m["yes_token"])
        if m.get("no_token"):
            tokens.append(m["no_token"])
    if tokens:
        volatility.register_tokens(tokens)
        from . import ws_client as _ws
        await _ws.subscribe_assets(tokens)

    log.info("scanner_refreshed", coin=coin, count=len(active))

    if active:
        first = active[0]
        log.info("scanner_first_market", coin=coin,
                 slug=first["slug"], question=first["question"][:80],
                 window_start=str(first["window_start"]),
                 window_end=str(first["window_end"]))
        try:
            await save_dashboard_state(f"scanner_{coin}", json.dumps({
                "count": len(active),
                "next_start": first["window_start"].isoformat() if first["window_start"] else None,
                "next_end": first["window_end"].isoformat() if first["window_end"] else None,
            }))
        except Exception as e:
            log.warning("scanner_state_save_failed", coin=coin, error=str(e))
    else:
        log.warning("scanner_no_markets", coin=coin)
        try:
            await write_event(None, "scanner_alert", coin, {"reason": "no_markets"})
            await save_dashboard_state(f"scanner_{coin}", json.dumps({"count": 0}))
        except Exception as e:
            log.warning("scanner_state_save_failed", coin=coin, error=str(e))


def get_upcoming_markets(coin: str, within_minutes: int = 30) -> list[dict]:
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=within_minutes)
    return [m for m in _market_cache.get(coin, [])
            if m["window_start"] and now <= m["window_start"] <= cutoff]


def get_next_windows(coin: str, n: int = 3) -> list[dict]:
    now = datetime.now(timezone.utc)
    return [m for m in _market_cache.get(coin, [])
            if m["window_end"] and m["window_end"] > now][:n]


def get_current_bggdsb_market(coin: str) -> dict | None:
    """Return the currently running 5-min window (window_start <= now <= window_end)."""
    now = datetime.now(timezone.utc)
    running = [
        m for m in _market_cache.get(coin, [])
        if m.get("window_start") and m.get("window_end")
        and m["window_start"] <= now <= m["window_end"]
    ]
    if not running:
        return None
    return max(running, key=lambda m: m["window_start"])


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


async def get_next_scalper_market(coin: str, filter_slug: str) -> dict | None:
    """Fetch the next upcoming market for the Stoplicht Scalper (uses custom filter_slug).

    This bypasses the per-coin _market_cache so the scalper can use a different
    duration slug (e.g. 'btc-updown-15m') without affecting the straddle bot.
    """
    markets = await _fetch_markets_for_coin(coin, filter_slug)
    now = datetime.now(timezone.utc)
    upcoming = [
        m for m in markets
        if m.get("window_start") and m["window_start"] > now
        and m.get("window_end") and m["window_end"] > now
    ]
    if not upcoming:
        return None
    upcoming.sort(key=lambda m: m["window_start"])
    market = upcoming[0]
    # Subscribe tokens so ws_client can provide real-time prices
    tokens = [t for t in (market.get("yes_token"), market.get("no_token")) if t]
    if tokens:
        from . import ws_client as _ws
        await _ws.subscribe_assets(tokens)
    return market


async def scanner_loop(interval_seconds: int = 60) -> None:
    while True:
        try:
            await refresh_markets()
        except Exception as e:
            log.error("scanner_loop_error", error=str(e))
        await asyncio.sleep(interval_seconds)
