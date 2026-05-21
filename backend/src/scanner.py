"""Multi-coin market scanner — discovers upcoming 5-min windows on Polymarket."""
import asyncio
from datetime import datetime, timezone, timedelta
import httpx

from .config_loader import CONFIG
from .logger import log, write_event

GAMMA_URL = CONFIG["polymarket"]["gamma_url"]

_COIN_VARIANTS: dict[str, list[str]] = {
    "BTC":  ["btc", "bitcoin"],
    "ETH":  ["eth", "ethereum", "ether"],
    "SOL":  ["sol", "solana"],
    "XRP":  ["xrp", "ripple"],
    "DOGE": ["doge", "dogecoin"],
}
_TIME_VARIANTS = ["5 min", "5min", "5-min", "5 m", "5m"]

_market_cache: dict[str, list[dict]] = {}
_last_refresh: dict[str, datetime] = {}


async def _fetch_markets_for_coin(coin: str) -> list[dict]:
    """
    Multi-strategy fetch. Tries /events (grouped recurring series) then /markets,
    with progressively broader tag filters. Stops at first strategy that finds results.
    """
    variants = _COIN_VARIANTS.get(coin, [coin.lower()])

    base = {"active": "true", "closed": "false", "limit": 100,
            "order": "startDate", "ascending": "true"}

    strategies = [
        # (endpoint, extra_params)
        ("/events",  {"tag": coin}),
        ("/events",  {"tag": coin.lower()}),
        ("/events",  {"tag": "crypto"}),
        ("/events",  {}),
        ("/markets", {"tag": coin}),
        ("/markets", {"tag": coin.lower()}),
        ("/markets", {"tag": "crypto"}),
        ("/markets", {}),
    ]

    seen: set[str] = set()
    markets: list[dict] = []

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for endpoint, extra in strategies:
                resp = await client.get(f"{GAMMA_URL}{endpoint}", params={**base, **extra})
                if resp.status_code != 200:
                    continue

                data = resp.json()
                raw = data if isinstance(data, list) else data.get("markets") or data.get("events") or []

                for item in raw:
                    # Events contain child markets; markets are direct
                    child_markets = item.get("markets") or [item]
                    event_text = (
                        (item.get("title") or item.get("question") or "")
                        + " "
                        + (item.get("slug") or "")
                    ).lower()

                    for m in child_markets:
                        mid = str(m.get("id") or m.get("conditionId") or "")
                        if not mid or mid in seen:
                            continue
                        q = (m.get("question") or "").lower()
                        slug = (m.get("slug") or "").lower()
                        text = q + " " + slug + " " + event_text

                        coin_hit = any(v in text for v in variants)
                        time_hit = any(t in text for t in _TIME_VARIANTS)

                        if coin_hit and time_hit:
                            seen.add(mid)
                            markets.append(_normalize_market(coin, m))

                if markets:
                    log.info("scanner_strategy_hit", coin=coin,
                             endpoint=endpoint, extra=extra, count=len(markets))
                    break
    except Exception as e:
        log.error("scanner_fetch_error", coin=coin, error=str(e))

    return markets


def _normalize_market(coin: str, raw: dict) -> dict:
    tokens = raw.get("tokens") or raw.get("clobTokenIds") or []
    yes_token = no_token = None
    if isinstance(tokens, list) and len(tokens) >= 2:
        yes_token = tokens[0]
        no_token = tokens[1]
    elif isinstance(tokens, dict):
        yes_token = tokens.get("yes") or tokens.get("YES")
        no_token = tokens.get("no") or tokens.get("NO")

    return {
        "coin": coin,
        "market_id": raw.get("id") or raw.get("conditionId"),
        "condition_id": raw.get("conditionId") or raw.get("condition_id"),
        "slug": raw.get("slug", ""),
        "question": raw.get("question", ""),
        "yes_token": yes_token,
        "no_token": no_token,
        "window_start": _parse_ts(raw.get("startDate") or raw.get("start_date")),
        "window_end": _parse_ts(raw.get("endDate") or raw.get("end_date")),
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
    coins = [coin] if coin else [c for c in CONFIG["coins"] if CONFIG["coins"][c]["enabled"]]
    await asyncio.gather(*[_refresh_coin(c) for c in coins])


async def _refresh_coin(coin: str) -> None:
    markets = await _fetch_markets_for_coin(coin)
    now = datetime.now(timezone.utc)
    upcoming = [m for m in markets
                if m["window_start"] and m["window_start"] > now - timedelta(minutes=5)]
    upcoming.sort(key=lambda m: m["window_start"])
    _market_cache[coin] = upcoming
    _last_refresh[coin] = now
    log.info("scanner_refreshed", coin=coin, count=len(upcoming))

    if not upcoming:
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
            if m["window_start"] and m["window_start"] > now - timedelta(minutes=1)][:n]


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
