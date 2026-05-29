"""Oracle external data feeds: Fear & Greed, Massive.com price momentum, Polymarket aggregate.

All feeds are cached to avoid excessive S3 requests. Recency weighting is applied
to time-series data so recent signals count more than older ones.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import io
import math
import os
import time
from datetime import datetime, timezone, timedelta
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


# ── Massive.com S3 price momentum ─────────────────────────────────────────────
# Replaces CryptoPanic: derives bullish/bearish sentiment from price momentum
# (7-day close vs. 30-day average) using Massive crypto day aggregates.
# Data is available ~11:00 AM ET the day after each trading day.
# Cached 6 hours — the file only updates once per day anyway.

_massive_cache: dict[str, Any] = {}
_massive_cache_ts: float = 0.0
_MASSIVE_TTL = 6 * 3600.0  # 6 hours

# Coin → list of ticker prefixes to match in the CSV
_COIN_TICKERS: dict[str, list[str]] = {
    "BTC":  ["BTC-USD", "BTC/USD", "BTCUSD", "XBT-USD"],
    "ETH":  ["ETH-USD", "ETH/USD", "ETHUSD"],
    "SOL":  ["SOL-USD", "SOL/USD", "SOLUSD"],
    "XRP":  ["XRP-USD", "XRP/USD", "XRPUSD"],
    "DOGE": ["DOGE-USD", "DOGE/USD", "DOGEUSD"],
}


def _get_massive_credentials() -> tuple[str, str]:
    """Returns (access_key_id, secret_access_key) from env or config."""
    key = (
        os.environ.get("MASSIVE_ACCESS_KEY_ID")
        or CONFIG.get("oracle", {}).get("feeds", {}).get("massive_access_key_id", "")
    )
    secret = (
        os.environ.get("MASSIVE_SECRET_ACCESS_KEY")
        or CONFIG.get("oracle", {}).get("feeds", {}).get("massive_secret_access_key", "")
    )
    return key, secret


def _s3_day_agg_key(date: datetime) -> str:
    """S3 object key for a given date: global_crypto/day_aggs_v1/YYYY/MM/YYYY-MM-DD.csv.gz"""
    return f"global_crypto/day_aggs_v1/{date:%Y}/{date:%m}/{date:%Y-%m-%d}.csv.gz"


def _download_day_agg_sync(key_id: str, secret: str, s3_key: str) -> bytes | None:
    """Download a single day-agg CSV.gz from Massive S3. Returns raw bytes or None."""
    try:
        import boto3
        from botocore.config import Config as BotoConfig

        session = boto3.Session(
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
        )
        s3 = session.client(
            "s3",
            endpoint_url="https://files.massive.com",
            config=BotoConfig(signature_version="s3v4"),
        )
        buf = io.BytesIO()
        s3.download_fileobj("flatfiles", s3_key, buf)
        return buf.getvalue()
    except Exception as exc:
        log.debug("massive_s3_download_failed", key=s3_key, error=str(exc))
        return None


def _parse_day_agg(raw: bytes) -> list[dict]:
    """Parse a gzip-compressed CSV day-agg file into a list of row dicts."""
    try:
        with gzip.open(io.BytesIO(raw), "rt") as f:
            reader = csv.DictReader(f)
            return list(reader)
    except Exception:
        return []


def _compute_momentum(rows: list[dict], coin: str) -> float | None:
    """Return price momentum score -1..+1 for a coin from a list of daily rows.

    Positive = recent closes above longer-term average (bullish).
    Negative = recent closes below longer-term average (bearish).
    """
    tickers = _COIN_TICKERS.get(coin, [f"{coin}-USD", f"{coin}/USD"])
    closes: list[float] = []
    for row in rows:
        ticker = row.get("ticker", "")
        if not any(ticker.upper().startswith(t.upper()) for t in tickers):
            continue
        try:
            closes.append(float(row["close"]))
        except (KeyError, ValueError, TypeError):
            pass

    if len(closes) < 4:
        return None

    # Most recent 3 days vs. the rest
    recent_avg = sum(closes[:3]) / 3
    baseline_avg = sum(closes) / len(closes)
    if baseline_avg == 0:
        return None

    raw_momentum = (recent_avg - baseline_avg) / baseline_avg  # e.g. +0.04 = +4%
    return max(-1.0, min(1.0, raw_momentum * 10))  # scale: 10% move → ±1.0


async def get_crypto_news_sentiment(coins: list[str] | None = None) -> dict:
    """Derive price-momentum-based sentiment from Massive.com day aggregates.

    Returns {coin: {"sentiment": "positive"|"negative"|"neutral"|"unknown",
                     "weighted_score": float,  # -1..+1
                     "top_headline": str|None}}

    Same interface as the former CryptoPanic feed — Oracle code is unchanged.
    Falls back to "unknown" when credentials are missing or S3 is unavailable.
    """
    global _massive_cache, _massive_cache_ts

    coins = coins or list(CONFIG.get("coins", {}).keys())

    key_id, secret = _get_massive_credentials()
    if not key_id or not secret:
        return {c: {"sentiment": "unknown", "weighted_score": 0.0, "top_headline": None}
                for c in coins}

    if time.monotonic() - _massive_cache_ts < _MASSIVE_TTL and _massive_cache:
        return _massive_cache

    # Download up to 7 recent trading days (skip today — data only available next day)
    now_utc = datetime.now(timezone.utc)
    all_rows: list[dict] = []

    def _fetch_days() -> list[dict]:
        rows: list[dict] = []
        for days_back in range(1, 8):
            date = now_utc - timedelta(days=days_back)
            s3_key = _s3_day_agg_key(date)
            raw = _download_day_agg_sync(key_id, secret, s3_key)
            if raw:
                rows.extend(_parse_day_agg(raw))
            if len({r.get("ticker") for r in rows}) > 5:
                break  # enough data
        return rows

    try:
        # Run blocking S3 downloads in a thread so we don't block the event loop
        all_rows = await asyncio.get_event_loop().run_in_executor(None, _fetch_days)
    except Exception as exc:
        log.warning("massive_fetch_failed", error=str(exc))
        return {c: {"sentiment": "unknown", "weighted_score": 0.0, "top_headline": None}
                for c in coins}

    result: dict[str, dict] = {}
    for coin in coins:
        score = _compute_momentum(all_rows, coin)
        if score is None:
            result[coin] = {"sentiment": "unknown", "weighted_score": 0.0, "top_headline": None}
            continue

        if score > 0.15:
            sentiment = "positive"
        elif score < -0.15:
            sentiment = "negative"
        else:
            sentiment = "neutral"

        # "top_headline" repurposed as a human-readable summary
        pct = score * 10  # reverse the ×10 scaling for display
        direction = "+" if pct >= 0 else ""
        headline = f"7d momentum: {direction}{pct:.1f}% vs 30d gemiddelde (Massive)"

        result[coin] = {
            "sentiment": sentiment,
            "weighted_score": round(score, 3),
            "top_headline": headline,
        }
        log.debug("massive_momentum", coin=coin, score=round(score, 3), sentiment=sentiment)

    _massive_cache = result
    _massive_cache_ts = time.monotonic()
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
