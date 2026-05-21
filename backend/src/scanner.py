"""Multi-coin market scanner — discovers upcoming 5-min windows on Polymarket."""
import asyncio
from datetime import datetime, timezone, timedelta
import httpx

from .config_loader import CONFIG
from .logger import log, write_event

GAMMA_URL = CONFIG["polymarket"]["gamma_url"]

# Slug prefix for each coin: Polymarket uses "{prefix}-updown-5m-{unix_ts}"
_COIN_SLUG_PREFIX: dict[str, list[str]] = {
    "BTC":  ["btc-"],
    "ETH":  ["eth-"],
    "SOL":  ["sol-"],
    "XRP":  ["xrp-"],
    "DOGE": ["doge-"],
}

# Fallback: coin keyword in question text (case-insensitive)
_COIN_QUESTION_VARIANTS: dict[str, list[str]] = {
    "BTC":  ["bitcoin"],
    "ETH":  ["ethereum", "ether"],
    "SOL":  ["solana"],
    "XRP":  ["xrp", "ripple"],
    "DOGE": ["dogecoin", "doge"],
}

_market_cache: dict[str, list[dict]] = {}
_last_refresh: dict[str, datetime] = {}

_5M_SLUG_MARKER = "updown-5m"


async def _fetch_all_5m_markets() -> dict[str, list[dict]]:
    """
    Single broad API call — no tag filter (Polymarket 5M markets have no tags).
    Identifies 5M markets by 'updown-5m' in slug, then assigns coin by slug prefix.
    Returns a dict coin -> list of normalised markets.
    """
    by_coin: dict[str, list[dict]] = {c: [] for c in _COIN_SLUG_PREFIX}
    params = {
        "limit": 500,
        "order": "startDate",
        "ascending": "false",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{GAMMA_URL}/markets", params=params)
            if resp.status_code != 200:
                log.error("scanner_api_error", status=resp.status_code)
                return by_coin

            data = resp.json()
            raw_list = data if isinstance(data, list) else data.get("markets", [])
            log.info("scanner_api_raw", total=len(raw_list))

            for m in raw_list:
                slug = (m.get("slug") or "").lower()
                question = (m.get("question") or "").lower()

                if _5M_SLUG_MARKER not in slug:
                    continue

                coin = _match_coin(slug, question)
                if coin and CONFIG["coins"][coin]["enabled"]:
                    by_coin[coin].append(_normalize_market(coin, m))

    except Exception as e:
        log.error("scanner_fetch_error", error=str(e))

    return by_coin


def _match_coin(slug: str, question: str) -> str | None:
    """Return the coin key whose slug prefix matches, or None."""
    for coin, prefixes in _COIN_SLUG_PREFIX.items():
        if any(slug.startswith(p) for p in prefixes):
            return coin
    # Fallback: question keyword match
    for coin, variants in _COIN_QUESTION_VARIANTS.items():
        if any(v in question for v in variants):
            return coin
    return None


def _normalize_market(coin: str, raw: dict) -> dict:
    tokens = raw.get("tokens") or raw.get("clobTokenIds") or []
    yes_token = no_token = None
    if isinstance(tokens, list) and len(tokens) >= 2:
        yes_token = tokens[0]
        no_token = tokens[1]
    elif isinstance(tokens, dict):
        yes_token = tokens.get("yes") or tokens.get("YES")
        no_token = tokens.get("no") or tokens.get("NO")

    # endDate = when the prediction window closes/resolves
    # window_start = endDate - 5 minutes (Polymarket 5M convention)
    window_end = _parse_ts(raw.get("endDate") or raw.get("end_date"))
    window_start = (window_end - timedelta(minutes=5)) if window_end else None

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
    """Refresh the market cache. One API call fetches all coins at once."""
    all_markets = await _fetch_all_5m_markets()
    now = datetime.now(timezone.utc)

    coins = [coin] if coin else list(_COIN_SLUG_PREFIX.keys())
    for c in coins:
        if not CONFIG["coins"][c]["enabled"]:
            continue
        markets = all_markets.get(c, [])
        # Keep markets whose prediction window hasn't ended yet and is within 24h
        active = [m for m in markets
                  if m["window_end"] and now - timedelta(minutes=1) < m["window_end"] < now + timedelta(hours=25)]
        active.sort(key=lambda m: m["window_end"] or now)
        _market_cache[c] = active
        _last_refresh[c] = now
        log.info("scanner_refreshed", coin=c, active=len(active))

        if active:
            # Log first match to verify slug/question shape
            first = active[0]
            log.info("scanner_first_market", coin=c,
                     slug=first["slug"], question=first["question"][:80],
                     window_start=str(first["window_start"]),
                     window_end=str(first["window_end"]))
        else:
            log.warning("scanner_no_markets", coin=c)
            await write_event(None, "scanner_alert", c, {"reason": "no_markets"})


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
    """Return the market whose entry window is open right now."""
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
