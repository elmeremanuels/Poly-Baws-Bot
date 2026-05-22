"""Multi-coin market scanner — discovers upcoming 5-min windows on Polymarket."""
import asyncio
import json
from datetime import datetime, timezone, timedelta
import httpx

from .config_loader import CONFIG
from .logger import log, write_event, save_dashboard_state
from . import volatility

GAMMA_URL = CONFIG["polymarket"]["gamma_url"]
COIN_FILTERS = {coin: cfg["market_filter"] for coin, cfg in CONFIG["coins"].items()}

_market_cache: dict[str, list[dict]] = {}
_last_refresh: dict[str, datetime] = {}


async def _fetch_markets_for_coin(coin: str, filter_slug: str) -> list[dict]:
    """Fetch currently-tradeable events from Gamma API; extract nested markets."""
    now = datetime.now(timezone.utc)
    end_min = now.isoformat()

    results = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            params = {
                "closed": "false",
                "limit": 500,
                "order": "endDate",
                "ascending": "true",
                "end_date_min": end_min,
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

    # Parse window_start from slug. Pattern: {coin}-updown-5m-{unix_epoch}
    window_start = None
    window_end = None
    parts = slug.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        try:
            window_start = datetime.fromtimestamp(int(parts[1]), tz=timezone.utc)
            window_end = window_start + timedelta(minutes=5)
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
    coins = [coin] if coin else [c for c in COIN_FILTERS if CONFIG["coins"][c]["enabled"]]
    # Sequential — parallel gather caused 5 simultaneous DB writes → database is locked
    for c in coins:
        await _refresh_coin(c)


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

    tokens = []
    for m in active:
        if m.get("yes_token"):
            tokens.append(m["yes_token"])
        if m.get("no_token"):
            tokens.append(m["no_token"])
    if tokens:
        volatility.register_tokens(tokens)

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
