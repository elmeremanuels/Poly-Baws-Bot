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
_TIME_VARIANTS = ["5 min", "5min", "5-min", "5 m", "5m", "/5m"]

_market_cache: dict[str, list[dict]] = {}
_last_refresh: dict[str, datetime] = {}


async def _fetch_markets_for_coin(coin: str) -> list[dict]:
    """
    Multi-strategy fetch across /events and /markets with progressively broader
    filters. Each strategy is tried in order; stops at the first that returns results.
    The last group of strategies drops the 5-min keyword requirement as a fallback.
    """
    variants = _COIN_VARIANTS.get(coin, [coin.lower()])

    base = {"active": "true", "closed": "false", "limit": 100,
            "order": "startDate", "ascending": "true"}

    # (endpoint, extra_params, require_time_keyword)
    strategies = [
        ("/events",  {"tag": coin},      True),
        ("/events",  {"tag": coin.lower()}, True),
        ("/events",  {"tag": "crypto"},  True),
        ("/events",  {},                 True),
        ("/markets", {"tag": coin},      True),
        ("/markets", {"tag": coin.lower()}, True),
        ("/markets", {"tag": "crypto"},  True),
        ("/markets", {},                 True),
        # Coin-only fallback (no time keyword) — catches markets that don't say "5m"
        ("/events",  {"tag": "crypto"},  False),
        ("/events",  {},                 False),
        ("/markets", {"tag": "crypto"},  False),
    ]

    seen: set[str] = set()
    markets: list[dict] = []

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for endpoint, extra, require_time in strategies:
                resp = await client.get(f"{GAMMA_URL}{endpoint}", params={**base, **extra})
                if resp.status_code != 200:
                    continue

                data = resp.json()
                raw = data if isinstance(data, list) else (
                    data.get("markets") or data.get("events") or []
                )

                for item in raw:
                    child_markets = item.get("markets") or [item]
                    # Parent event title/slug enriches the matching context
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

                        if coin_hit and (time_hit or not require_time):
                            seen.add(mid)
                            norm = _normalize_market(coin, m)
                            markets.append(norm)
                            # Debug: log the first new market so we can inspect the API shape
                            if len(markets) == 1:
                                log.info(
                                    "scanner_first_match",
                                    coin=coin,
                                    endpoint=endpoint,
                                    require_time=require_time,
                                    slug=norm["slug"],
                                    question=norm["question"][:80],
                                    window_start=str(norm["window_start"]),
                                    window_end=str(norm["window_end"]),
                                )

                if markets:
                    log.info("scanner_strategy_hit", coin=coin, endpoint=endpoint,
                             require_time=require_time, count=len(markets))
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

    # Keep markets that haven't ended yet (or whose end time is unknown).
    # Fine-grained timing (entry window, cutoff) is handled by get_tradeable_market().
    # We deliberately do NOT filter on window_start here — Polymarket's recurring
    # event series has a startDate equal to when the series was created (months ago).
    active = [
        m for m in markets
        if m["window_end"] is None or m["window_end"] > now - timedelta(minutes=1)
    ]
    active.sort(key=lambda m: m["window_start"] or now)
    _market_cache[coin] = active
    _last_refresh[coin] = now
    log.info("scanner_refreshed", coin=coin, found=len(markets), active=len(active))

    if not active:
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
