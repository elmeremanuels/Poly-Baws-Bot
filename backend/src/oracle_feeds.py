"""Oracle external data feeds: Fear & Greed, CryptoPanic news, Polymarket aggregate.

All feeds are cached to avoid rate-limit issues. Recency weighting is applied
to time-series data so recent signals count more than older ones.
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any

import httpx

from .config_loader import CONFIG
from .logger import log

# ── Recency weighting ──────────────────────────────────────────────────────────

def recency_weight(hours_ago: float, halflife_hours: float = 6.0) -> float:
    """Exponential decay: 0h=1.0, 6h=0.5, 12h=0.25, 24h=0.06."""
    return math.exp(-hours_ago * math.log(2) / halflife_hours)


# ── Fear & Greed cache ─────────────────────────────────────────────────────────

_fg_cache: dict[str, Any] = {}
_fg_cache_ts: float = 0.0
_FG_TTL = 3600.0  # 1 hour


async def get_fear_greed() -> dict:
    """Fetch Fear & Greed index from alternative.me. Cached 1 hour."""
    global _fg_cache, _fg_cache_ts
    if time.monotonic() - _fg_cache_ts < _FG_TTL and _fg_cache:
        return _fg_cache
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get("https://api.alternative.me/fng/?limit=1")
            resp.raise_for_status()
            data = resp.json()
        entry = data["data"][0]
        _fg_cache = {
            "value": int(entry["value"]),
            "label": entry["value_classification"],
        }
        _fg_cache_ts = time.monotonic()
        log.debug("fear_greed_fetched", value=_fg_cache["value"], label=_fg_cache["label"])
    except Exception as exc:
        log.warning("fear_greed_fetch_failed", error=str(exc))
        if not _fg_cache:
            _fg_cache = {"value": 50, "label": "Neutral"}
    return _fg_cache


# ── CryptoPanic news cache ─────────────────────────────────────────────────────

_news_cache: dict[str, Any] = {}
_news_cache_ts: float = 0.0
_NEWS_TTL = 600.0  # 10 minutes


async def get_crypto_news_sentiment(coins: list[str] | None = None) -> dict:
    """Fetch recent news from CryptoPanic and return recency-weighted sentiment per coin.

    Returns {coin: {"sentiment": "positive"|"negative"|"neutral"|"unknown",
                     "weighted_score": float,  # -1..+1
                     "top_headline": str|None}}
    """
    global _news_cache, _news_cache_ts
    token = CONFIG.get("oracle", {}).get("feeds", {}).get("cryptopanic_token", "")
    if not token:
        return {c: {"sentiment": "unknown", "weighted_score": 0.0, "top_headline": None}
                for c in (coins or [])}

    if time.monotonic() - _news_cache_ts < _NEWS_TTL and _news_cache:
        return _news_cache

    coins = coins or list(CONFIG.get("coins", {}).keys())
    currencies = ",".join(coins)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://cryptopanic.com/api/v1/posts/",
                params={"auth_token": token, "currencies": currencies,
                        "kind": "news", "limit": 50},
            )
            resp.raise_for_status()
            posts = resp.json().get("results", [])
    except Exception as exc:
        log.warning("cryptopanic_fetch_failed", error=str(exc))
        return {c: {"sentiment": "unknown", "weighted_score": 0.0, "top_headline": None}
                for c in coins}

    now_ts = time.time()
    result: dict[str, dict] = {}

    for coin in coins:
        coin_posts = [p for p in posts if coin in (p.get("currencies") or [])]
        if not coin_posts:
            result[coin] = {"sentiment": "unknown", "weighted_score": 0.0, "top_headline": None}
            continue

        _SENT = {"positive": 1.0, "negative": -1.0, "neutral": 0.0, None: 0.0}
        weighted_sum = 0.0
        weight_total = 0.0
        top_headline = None

        for p in coin_posts[:20]:
            published = p.get("published_at", "")
            try:
                from datetime import datetime, timezone
                pub_ts = datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp()
                hours_ago = max(0, (now_ts - pub_ts) / 3600)
            except Exception:
                hours_ago = 6.0
            w = recency_weight(hours_ago)
            sent_val = _SENT.get(p.get("votes", {}).get("positive") and "positive"
                                 or p.get("votes", {}).get("negative") and "negative"
                                 or "neutral", 0.0)
            # Use explicit kind field if available
            kind = (p.get("kind") or "").lower()
            if kind in ("positive", "bullish"):
                sent_val = 1.0
            elif kind in ("negative", "bearish"):
                sent_val = -1.0

            weighted_sum += w * sent_val
            weight_total += w
            if top_headline is None:
                top_headline = p.get("title")

        score = weighted_sum / weight_total if weight_total > 0 else 0.0
        if score > 0.15:
            sentiment = "positive"
        elif score < -0.15:
            sentiment = "negative"
        else:
            sentiment = "neutral"

        result[coin] = {
            "sentiment": sentiment,
            "weighted_score": round(score, 3),
            "top_headline": top_headline,
        }

    _news_cache = result
    _news_cache_ts = time.monotonic()
    return result


# ── Polymarket aggregate sentiment ────────────────────────────────────────────

_pm_cache: dict[str, Any] = {}
_pm_cache_ts: float = 0.0
_PM_TTL = 300.0  # 5 minutes


async def get_polymarket_coin_sentiment(coin: str) -> dict:
    """Query Gamma API for all UP/DOWN markets for a coin; aggregate YES-price.

    Returns: {avg_yes_price, n_markets, implied_direction: "UP"|"DOWN"|"NEUTRAL"}
    """
    global _pm_cache, _pm_cache_ts
    cache_key = coin
    if (time.monotonic() - _pm_cache_ts < _PM_TTL
            and cache_key in _pm_cache):
        return _pm_cache[cache_key]

    gamma_base = CONFIG.get("polymarket", {}).get("gamma_url", "https://gamma-api.polymarket.com")
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                f"{gamma_base}/markets",
                params={"tag": coin.lower(), "active": "true", "limit": 20},
            )
            resp.raise_for_status()
            markets = resp.json()
    except Exception as exc:
        log.debug("polymarket_aggregate_failed", coin=coin, error=str(exc))
        result = {"avg_yes_price": 0.5, "n_markets": 0, "implied_direction": "NEUTRAL"}
        _pm_cache[cache_key] = result
        return result

    yes_prices = []
    for m in markets:
        question = (m.get("question") or "").lower()
        if "up" in question or "above" in question or "higher" in question:
            price = m.get("outcomePrices")
            if isinstance(price, list) and len(price) >= 1:
                try:
                    yes_prices.append(float(price[0]))
                except (ValueError, TypeError):
                    pass

    if not yes_prices:
        result = {"avg_yes_price": 0.5, "n_markets": 0, "implied_direction": "NEUTRAL"}
    else:
        avg = sum(yes_prices) / len(yes_prices)
        direction = "UP" if avg > 0.55 else ("DOWN" if avg < 0.45 else "NEUTRAL")
        result = {
            "avg_yes_price": round(avg, 3),
            "n_markets": len(yes_prices),
            "implied_direction": direction,
        }

    _pm_cache[cache_key] = result
    _pm_cache_ts = time.monotonic()
    return result
