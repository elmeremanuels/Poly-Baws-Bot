"""Real-time market signals: order flow imbalance, funding rate, liquidation proxy.

Phase 1 of the pricing-model roadmap.  Three signals derived from Binance:

  OFI   — Order Flow Imbalance: buy_volume / total_volume over a rolling window.
           > 0.55 = net buying pressure;  < 0.45 = net selling pressure.
           Source: aggTrade stream (m=False → taker is buyer → BUY-initiated).

  FR    — Funding Rate: latest perpetual funding rate.
           Extreme positive → leveraged longs crowded → mean-reversion risk.
           Source: Binance FAPI fundingRate endpoint, polled every 5 min.

  LIQ   — Liquidation Proxy: volume spike ratio (recent 30 s / baseline).
           > 2.0 = unusual volume → possible cascade liquidation.
           Source: same aggTrade buffer.

Conviction score combines all three into a (direction, score 0–1) tuple.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque

import httpx

from .config_loader import CONFIG
from .logger import log

# ── Symbol mapping ─────────────────────────────────────────────────────────────

_COIN_TO_PERP: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
    "DOGE": "DOGEUSDT",
}

_FAPI_BASE = "https://fapi.binance.com"

# ── In-memory buffers ──────────────────────────────────────────────────────────

# {coin: deque of (unix_ts, qty_float, is_buy: bool)}
_trades: dict[str, deque] = {}
_TRADE_WINDOW = 300  # keep 5 minutes of aggTrade data

# {coin: latest funding rate float}
_funding_rates: dict[str, float] = {}


# ── aggTrade recording ─────────────────────────────────────────────────────────

def record_trade(coin: str, qty: float, is_buy: bool) -> None:
    """Store one aggTrade event (called from asset_price_feed.run_trade_stream)."""
    buf = _trades.setdefault(coin, deque())
    now = time.time()
    buf.append((now, qty, is_buy))
    cutoff = now - _TRADE_WINDOW
    while buf and buf[0][0] < cutoff:
        buf.popleft()


# ── Signal accessors ───────────────────────────────────────────────────────────

def get_order_flow_imbalance(coin: str, window_secs: float = 60.0) -> float | None:
    """Return buy_volume / total_volume for the last window_secs seconds.

    Returns None when fewer than 10 trades are available (too noisy).
    """
    buf = _trades.get(coin)
    if not buf:
        return None
    cutoff = time.time() - window_secs
    buy_vol = 0.0
    total_vol = 0.0
    for ts, qty, is_buy in buf:
        if ts < cutoff:
            continue
        total_vol += qty
        if is_buy:
            buy_vol += qty
    if total_vol < 1e-8:
        return None
    n = sum(1 for ts, _, _ in buf if ts >= cutoff)
    if n < 10:
        return None
    return round(buy_vol / total_vol, 4)


def get_funding_rate(coin: str) -> float | None:
    """Return the latest perpetual funding rate, or None if not yet fetched."""
    return _funding_rates.get(coin)


def get_liquidation_proxy(coin: str, spike_window_secs: float = 30.0) -> float | None:
    """Volume spike ratio: recent_vol / baseline_vol_per_30s.

    > 2.0 suggests abnormal activity (possible cascade liquidation).
    Returns None when insufficient history.
    """
    buf = _trades.get(coin)
    if not buf:
        return None
    now = time.time()
    recent_cutoff = now - spike_window_secs
    baseline_cutoff = now - _TRADE_WINDOW

    recent_vol = sum(qty for ts, qty, _ in buf if ts >= recent_cutoff)
    # Baseline = average 30s volume over the full 5-min window
    baseline_total = sum(qty for ts, qty, _ in buf if ts >= baseline_cutoff)
    baseline_slots = _TRADE_WINDOW / spike_window_secs  # 10 slots of 30s in 5min
    baseline_per_slot = baseline_total / baseline_slots

    if baseline_per_slot < 1e-8:
        return None
    return round(recent_vol / baseline_per_slot, 3)


def get_all_signals(coin: str) -> dict:
    """Return snapshot of all signals for a coin (used for stamping on trades)."""
    ofi = get_order_flow_imbalance(coin)
    fr = get_funding_rate(coin)
    liq = get_liquidation_proxy(coin)
    conviction, conviction_score = get_conviction(coin)
    return {
        "ofi": ofi,
        "funding_rate": fr,
        "liq_proxy": liq,
        "conviction": conviction,
        "conviction_score": conviction_score,
    }


def get_conviction(coin: str) -> tuple[str | None, float]:
    """Combine OFI + funding rate into (direction, certainty 0–1).

    Direction: "UP", "DOWN", or None.
    Score: 0.0 = no signal, 1.0 = all signals aligned strongly.

    Rules:
      OFI > 0.55 → bullish raw signal (+score)
      OFI < 0.45 → bearish raw signal (+score)
      Funding rate > 0.001 (0.1%) → contrarian bearish pressure (-slight bullish, +bearish)
      Funding rate < -0.001       → contrarian bullish pressure (+slight bullish)
      Liquidation proxy > 2.0 → amplify direction signal (+0.1 bonus)
    """
    ofi = get_order_flow_imbalance(coin)
    fr = get_funding_rate(coin)
    liq = get_liquidation_proxy(coin)

    bull_score = 0.0
    bear_score = 0.0

    if ofi is not None:
        if ofi > 0.55:
            bull_score += (ofi - 0.55) / 0.45  # 0→1 as ofi goes 0.55→1.0
        elif ofi < 0.45:
            bear_score += (0.45 - ofi) / 0.45

    if fr is not None:
        if fr > 0.001:
            # Crowded longs → contrarian bearish
            bear_score += min(0.3, (fr - 0.001) / 0.005)
        elif fr < -0.001:
            bull_score += min(0.3, (-fr - 0.001) / 0.005)

    if liq is not None and liq > 2.0:
        bonus = min(0.10, (liq - 2.0) * 0.05)
        if bull_score >= bear_score:
            bull_score += bonus
        else:
            bear_score += bonus

    max_score = max(bull_score, bear_score)
    if max_score < 0.05:
        return None, 0.0

    direction = "UP" if bull_score >= bear_score else "DOWN"
    return direction, round(min(1.0, max_score), 3)


# ── aggTrade REST poller (replaces WebSocket stream) ──────────────────────────

_SPOT_REST = "https://api.binance.com/api/v3"
_POLL_INTERVAL = 10.0  # seconds between background aggTrade polls (10s is enough for rolling OFI)

# Per-coin last seen aggTradeId — shared between background loop and on-demand refresh
_last_trade_id: dict[str, int] = {}


async def refresh_ofi(coin: str) -> None:
    """Fetch the latest aggTrades for one coin right now (on-demand).

    Called directly before signal stamping at entry and trigger time so the
    OFI value is always ≤1 second old at the exact moment it matters, regardless
    of where the background poll cycle is.
    """
    if coin not in _COIN_TO_PERP:
        return
    symbol = _COIN_TO_PERP[coin]
    try:
        params: dict = {"symbol": symbol, "limit": 500}
        last = _last_trade_id.get(coin)
        if last:
            params["fromId"] = last + 1
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{_SPOT_REST}/aggTrades", params=params)
            resp.raise_for_status()
            trades = resp.json()
        if not trades:
            return
        for t in trades:
            record_trade(coin, float(t["q"]), not bool(t["m"]))
        _last_trade_id[coin] = int(trades[-1]["a"])
    except Exception as e:
        log.warning("ofi_refresh_failed", coin=coin, error=str(e))


async def run_trade_poll_loop() -> None:
    """Poll Binance spot aggTrades REST endpoint every 5s per coin for OFI.

    Replaces the WebSocket aggTrade stream which is blocked (HTTP 451) for
    datacenter IPs.  Tracks the last seen aggTradeId per coin so each trade
    is recorded exactly once.

    aggTrade fields:
      a  — aggregate trade ID (used for fromId pagination)
      q  — quantity (base asset)
      m  — is buyer the market maker? True = sell-initiated, False = buy-initiated
    """
    coins = [c for c in CONFIG.get("coins", {}).keys() if c in _COIN_TO_PERP]
    log.info("trade_poll_loop_starting", coins=coins)

    async with httpx.AsyncClient(timeout=10) as client:
        # Seed: fetch last 500 trades per coin to pre-fill the OFI buffer
        for coin in coins:
            symbol = _COIN_TO_PERP[coin]
            try:
                resp = await client.get(
                    f"{_SPOT_REST}/aggTrades",
                    params={"symbol": symbol, "limit": 500},
                )
                resp.raise_for_status()
                trades = resp.json()
                for t in trades:
                    record_trade(coin, float(t["q"]), not bool(t["m"]))
                if trades:
                    _last_trade_id[coin] = int(trades[-1]["a"])
                log.info("trade_poll_seeded", coin=coin, n=len(trades))
            except Exception as e:
                log.warning("trade_poll_seed_failed", coin=coin, error=str(e))

        # Background polling loop — keeps rolling buffer current between on-demand refreshes
        while True:
            await asyncio.sleep(_POLL_INTERVAL)
            for coin in coins:
                symbol = _COIN_TO_PERP[coin]
                try:
                    params: dict = {"symbol": symbol, "limit": 500}
                    last = _last_trade_id.get(coin)
                    if last:
                        params["fromId"] = last + 1
                    resp = await client.get(f"{_SPOT_REST}/aggTrades", params=params)
                    resp.raise_for_status()
                    trades = resp.json()
                    if not trades:
                        continue
                    for t in trades:
                        record_trade(coin, float(t["q"]), not bool(t["m"]))
                    _last_trade_id[coin] = int(trades[-1]["a"])
                except Exception as e:
                    log.warning("trade_poll_failed", coin=coin, error=str(e))


# ── Funding rate background poller ─────────────────────────────────────────────

async def seed_funding_rates() -> None:
    """Fetch current funding rates once on startup."""
    coins = [c for c in CONFIG.get("coins", {}).keys() if c in _COIN_TO_PERP]
    async with httpx.AsyncClient(timeout=10) as client:
        for coin in coins:
            symbol = _COIN_TO_PERP[coin]
            try:
                resp = await client.get(
                    f"{_FAPI_BASE}/fapi/v1/premiumIndex",
                    params={"symbol": symbol},
                )
                resp.raise_for_status()
                data = resp.json()
                rate = float(data.get("lastFundingRate", 0))
                _funding_rates[coin] = rate
                log.info("funding_rate_seeded", coin=coin, rate=rate)
            except Exception as e:
                log.warning("funding_rate_seed_failed", coin=coin, error=str(e))


async def funding_rate_loop() -> None:
    """Poll Binance futures funding rates every 5 minutes."""
    await seed_funding_rates()
    coins = [c for c in CONFIG.get("coins", {}).keys() if c in _COIN_TO_PERP]
    while True:
        await asyncio.sleep(300)
        async with httpx.AsyncClient(timeout=10) as client:
            for coin in coins:
                symbol = _COIN_TO_PERP[coin]
                try:
                    resp = await client.get(
                        f"{_FAPI_BASE}/fapi/v1/premiumIndex",
                        params={"symbol": symbol},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    rate = float(data.get("lastFundingRate", 0))
                    _funding_rates[coin] = rate
                    log.debug("funding_rate_updated", coin=coin, rate=rate)
                except Exception as e:
                    log.warning("funding_rate_poll_failed", coin=coin, error=str(e))
