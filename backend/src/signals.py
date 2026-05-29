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

# Perpetuals aggTrade buffer — separate from spot (same structure)
_perp_trades: dict[str, deque] = {}
_perp_last_trade_id: dict[str, int] = {}

# {coin: latest funding rate float}
_funding_rates: dict[str, float] = {}

# {coin: latest long/short ratio float} — accounts long / accounts short
_ls_ratios: dict[str, float] = {}


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


def record_perp_trade(coin: str, qty: float, is_buy: bool) -> None:
    """Store one perpetual aggTrade event."""
    buf = _perp_trades.setdefault(coin, deque())
    now = time.time()
    buf.append((now, qty, is_buy))
    cutoff = now - _TRADE_WINDOW
    while buf and buf[0][0] < cutoff:
        buf.popleft()


def get_perp_order_flow_imbalance(coin: str, window_secs: float = 60.0) -> float | None:
    """OFI from Binance perpetuals aggTrades.

    Futures traders lead spot price discovery — used as confirmation alongside
    spot OFI. Same formula, slightly lower weight in get_conviction().
    """
    buf = _perp_trades.get(coin)
    if not buf:
        return None
    cutoff = time.time() - window_secs
    buy_vol = total_vol = 0.0
    n = 0
    for ts, qty, is_buy in buf:
        if ts < cutoff:
            continue
        total_vol += qty
        if is_buy:
            buy_vol += qty
        n += 1
    if total_vol < 1e-8 or n < 10:
        return None
    return round(buy_vol / total_vol, 4)


def get_funding_rate(coin: str) -> float | None:
    """Return the latest perpetual funding rate, or None if not yet fetched."""
    return _funding_rates.get(coin)


def get_long_short_ratio(coin: str) -> float | None:
    """Return the latest global long/short account ratio, or None if not fetched.

    > 1.5 = >60% accounts long  → contrarian bearish pressure
    < 0.67 = >60% accounts short → contrarian bullish pressure
    """
    return _ls_ratios.get(coin)


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


def get_conviction(
    coin: str,
    yes_token: str | None = None,
    no_token: str | None = None,
) -> tuple[str | None, float]:
    """Combine all available signals into (direction, certainty 0–1).

    Direction: "UP", "DOWN", or None.
    Score: 0.0 = no signal, 1.0 = all signals aligned strongly.

    Signals (in order):
      OFI spot (Binance)          → ±0..1.0
      OFI perp (Binance futures)  → ±0..0.30  confirmation, half-weight
      Funding rate (contrarian)   → ±0..0.30
      Long/Short ratio (contrarian)→ ±0..0.15
      Liquidation proxy           → ±0..0.10 amplifier
      Price position (mean-rev)   → ±0..0.15
      Recent drift 5min           → ±0..0.20
      Polymarket depth imbalance  → ±0..0.15  (when tokens provided)
      Regime multiplier           → ×0.75..1.15
    """
    ofi = get_order_flow_imbalance(coin)
    perp_ofi = get_perp_order_flow_imbalance(coin)
    fr = get_funding_rate(coin)
    ls = get_long_short_ratio(coin)
    liq = get_liquidation_proxy(coin)

    # Lazy import to avoid circular dependency at module level
    from . import regime as _regime

    bull_score = 0.0
    bear_score = 0.0

    if ofi is not None:
        if ofi > 0.55:
            bull_score += (ofi - 0.55) / 0.45  # 0→1 as ofi goes 0.55→1.0
        elif ofi < 0.45:
            bear_score += (0.45 - ofi) / 0.45

    # Perpetuals OFI — futures lead spot; half-weight so spot remains primary
    if perp_ofi is not None:
        if perp_ofi > 0.55:
            bull_score += min(0.30, (perp_ofi - 0.55) / 0.45 * 0.50)
        elif perp_ofi < 0.45:
            bear_score += min(0.30, (0.45 - perp_ofi) / 0.45 * 0.50)

    if fr is not None:
        if fr > 0.001:
            # Crowded longs → contrarian bearish
            bear_score += min(0.3, (fr - 0.001) / 0.005)
        elif fr < -0.001:
            bull_score += min(0.3, (-fr - 0.001) / 0.005)

    # Long/Short account ratio — contrarian: extreme crowding precedes mean reversion
    if ls is not None:
        if ls > 1.5:    # >60% accounts long → contrarian bearish
            bear_score += min(0.15, (ls - 1.5) / 1.5)
        elif ls < 0.67:  # >60% accounts short → contrarian bullish
            bull_score += min(0.15, (0.67 - ls) / 0.67)

    if liq is not None and liq > 2.0:
        bonus = min(0.10, (liq - 2.0) * 0.05)
        if bull_score >= bear_score:
            bull_score += bonus
        else:
            bear_score += bonus

    # Price position within 30-min range — mean-reversion signal
    price_stats = _regime.get_asset_price_stats(coin)
    if price_stats:
        pp = price_stats.get("price_position")
        if pp is not None:
            if pp < 0.20:
                bull_score += 0.15 * (0.20 - pp) / 0.20
            elif pp > 0.80:
                bear_score += 0.15 * (pp - 0.80) / 0.20

    # Recent price drift (last 5 min) — actual observed direction, not inferred
    # Threshold 0.0005/min ≈ 0.25% over 5 min; capped contribution at 0.20
    drift = get_recent_drift_1m(coin)
    if drift is not None:
        _drift_threshold = 0.0005
        if drift > _drift_threshold:
            bull_score += min(0.20, (drift - _drift_threshold) / 0.004)
        elif drift < -_drift_threshold:
            bear_score += min(0.20, (-drift - _drift_threshold) / 0.004)

    # Polymarket CLOB depth imbalance — YES bids vs NO bids shows which side the
    # market is accumulating, independent of OFI on Binance spot
    if yes_token and no_token:
        from . import ws_client as _ws
        yes_bids = sum(float(v) for v in _ws.get_orderbook(yes_token).get("bids", {}).values())
        no_bids  = sum(float(v) for v in _ws.get_orderbook(no_token).get("bids", {}).values())
        total_bids = yes_bids + no_bids
        if total_bids >= 5.0:  # ignore trivially thin books
            depth_bias = (yes_bids - no_bids) / total_bids  # -1..+1
            if depth_bias > 0.10:
                bull_score += min(0.15, depth_bias * 0.20)
            elif depth_bias < -0.10:
                bear_score += min(0.15, -depth_bias * 0.20)

    # Regime multiplier — backtest shows TRENDING has worst P&L despite highest win rate
    # (fast peg_cross losses dominate in trending markets). RANGING is the best regime.
    current_regime = _regime.get_current_regime(coin)
    multiplier = {
        "TRENDING": 0.85,   # was 1.20 — amplified wrong-side peg_cross losses
        "BREAKOUT": 0.90,   # was 1.15 — same issue as TRENDING
        "CHOPPY": 0.75,     # was 0.80 — correct direction, minor tightening
        "RANGING": 1.15,    # was 1.00 — best P&L regime, reward it
        "NORMAL": 1.0,
    }.get(current_regime, 1.0)
    bull_score *= multiplier
    bear_score *= multiplier

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

        # Seed perp aggTrades buffer
        async with httpx.AsyncClient(timeout=10) as perp_client:
            for coin in coins:
                symbol = _COIN_TO_PERP[coin]
                try:
                    resp = await perp_client.get(
                        f"{_FAPI_BASE}/fapi/v1/aggTrades",
                        params={"symbol": symbol, "limit": 500},
                    )
                    resp.raise_for_status()
                    trades = resp.json()
                    for t in trades:
                        record_perp_trade(coin, float(t["q"]), not bool(t["m"]))
                    if trades:
                        _perp_last_trade_id[coin] = int(trades[-1]["a"])
                    log.info("perp_trade_poll_seeded", coin=coin, n=len(trades))
                except Exception as e:
                    log.warning("perp_trade_poll_seed_failed", coin=coin, error=str(e))

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

            # Poll perp aggTrades alongside spot
            async with httpx.AsyncClient(timeout=10) as perp_client:
                for coin in coins:
                    symbol = _COIN_TO_PERP[coin]
                    try:
                        params: dict = {"symbol": symbol, "limit": 500}
                        last = _perp_last_trade_id.get(coin)
                        if last:
                            params["fromId"] = last + 1
                        resp = await perp_client.get(
                            f"{_FAPI_BASE}/fapi/v1/aggTrades", params=params
                        )
                        resp.raise_for_status()
                        trades = resp.json()
                        if not trades:
                            continue
                        for t in trades:
                            record_perp_trade(coin, float(t["q"]), not bool(t["m"]))
                        _perp_last_trade_id[coin] = int(trades[-1]["a"])
                    except Exception as e:
                        log.warning("perp_trade_poll_failed", coin=coin, error=str(e))


# ── Funding rate background poller ─────────────────────────────────────────────

async def _fetch_ls_ratio(client: httpx.AsyncClient, coin: str) -> None:
    """Fetch global long/short account ratio for one coin and cache it."""
    symbol = _COIN_TO_PERP.get(coin)
    if not symbol:
        return
    try:
        resp = await client.get(
            f"{_FAPI_BASE}/futures/data/globalLongShortAccountRatio",
            params={"symbol": symbol, "period": "5m", "limit": 1},
        )
        resp.raise_for_status()
        data = resp.json()
        if data:
            ratio = float(data[0]["longShortRatio"])
            _ls_ratios[coin] = ratio
            log.debug("ls_ratio_updated", coin=coin, ratio=ratio)
    except Exception as e:
        log.warning("ls_ratio_fetch_failed", coin=coin, error=str(e))


async def seed_funding_rates() -> None:
    """Fetch current funding rates and L/S ratios once on startup."""
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
            await _fetch_ls_ratio(client, coin)


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
                await _fetch_ls_ratio(client, coin)


# ── Pricing Model v1: Black-Scholes binary option ─────────────────────────────
import math as _math


def _norm_cdf(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * _math.erfc(-x / _math.sqrt(2))


def get_realized_vol_1m(coin: str) -> float | None:
    """Realized per-minute volatility from the Binance spot price buffer.

    Computes std dev of consecutive log returns, scaled from the buffer's
    native sample interval (~5s) to per-minute using sqrt(60/interval).
    Returns None when fewer than 5 samples are available.
    """
    try:
        from . import regime as _regime
        buf = _regime._asset_prices.get(coin)
        if not buf or len(buf) < 5:
            return None
        items = list(buf)
        returns = [
            _math.log(items[i][1] / items[i - 1][1])
            for i in range(1, len(items))
            if items[i - 1][1] > 0 and items[i][1] > 0
        ]
        if len(returns) < 4:
            return None
        mean_r = sum(returns) / len(returns)
        var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        vol_per_sample = _math.sqrt(var_r)
        # Scale from per-sample to per-minute
        total_time = items[-1][0] - items[0][0]
        avg_interval = total_time / max(len(items) - 1, 1)
        return vol_per_sample * _math.sqrt(60.0 / max(1.0, avg_interval))
    except Exception:
        return None


def get_recent_drift_1m(coin: str, lookback_secs: float = 300.0) -> float | None:
    """Recent directional drift as average log-return per minute.

    Uses the last lookback_secs of spot prices. Positive = uptrend.
    """
    try:
        import time
        from . import regime as _regime
        buf = _regime._asset_prices.get(coin)
        if not buf:
            return None
        now = time.time()
        cutoff = now - lookback_secs
        recent = [(ts, p) for ts, p in buf if ts >= cutoff]
        if len(recent) < 3:
            return None
        start_p, end_p = recent[0][1], recent[-1][1]
        elapsed_mins = (recent[-1][0] - recent[0][0]) / 60.0
        if start_p <= 0 or end_p <= 0 or elapsed_mins < 0.1:
            return None
        return _math.log(end_p / start_p) / elapsed_mins
    except Exception:
        return None


def theoretical_price(side: str, coin: str, t_remaining_secs: float) -> float | None:
    """Binary option theoretical value via log-normal model.

    Returns P(asset UP at window end) given current realized volatility and recent drift.
    side="YES" → P(UP); side="NO" → P(DOWN) = 1 - P(UP).
    Returns None when insufficient data.
    """
    if t_remaining_secs <= 0:
        return None
    vol = get_realized_vol_1m(coin)
    if vol is None or vol < 1e-6:
        return None
    drift = get_recent_drift_1m(coin) or 0.0
    t_mins = t_remaining_secs / 60.0
    sigma_t = vol * _math.sqrt(t_mins)
    if sigma_t < 1e-8:
        return None
    d = (drift * t_mins) / sigma_t
    p_up = max(0.01, min(0.99, _norm_cdf(d)))
    return round(p_up if side == "YES" else 1.0 - p_up, 4)


def get_edge(
    coin: str,
    side: str,
    polymarket_price: float,
    t_remaining_secs: float,
) -> float | None:
    """Edge = theoretical_price - polymarket_price.

    Positive means Polymarket is underpricing this outcome → confirmed buy signal.
    Returns None when pricing model can't compute (insufficient data).
    """
    theo = theoretical_price(side, coin, t_remaining_secs)
    if theo is None:
        return None
    return round(theo - polymarket_price, 4)
