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

# ── Symbol mapping (Kraken — Binance is geo-blocked in DE/EU) ──────────────────

_COIN_TO_KRAKEN: dict[str, str] = {
    "BTC":  "XBTUSD",
    "ETH":  "ETHUSD",
    "SOL":  "SOLUSD",
    "XRP":  "XRPUSD",
    "DOGE": "XDGEUSD",
}

_COIN_TO_KRAKEN_FUT: dict[str, str] = {
    "BTC":  "PF_XBTUSD",
    "ETH":  "PF_ETHUSD",
    "SOL":  "PF_SOLUSD",
    "XRP":  "PF_XRPUSD",
    "DOGE": "PF_DOGEUSD",
}

# Backward-compat alias used elsewhere in this file
_COIN_TO_PERP = _COIN_TO_KRAKEN

_KRAKEN_REST = "https://api.kraken.com/0/public"
_KRAKEN_FUT  = "https://futures.kraken.com/derivatives/api/v3"

# ── In-memory buffers ──────────────────────────────────────────────────────────

# {coin: deque of (unix_ts, qty_float, is_buy: bool)}
_trades: dict[str, deque] = {}
_TRADE_WINDOW = 300  # keep 5 minutes of aggTrade data

# Perpetuals aggTrade buffer — separate from spot (same structure)
_perp_trades: dict[str, deque] = {}
_perp_last_trade_id: dict[str, int] = {}

# Kraken pagination state (nanosecond timestamps as strings)
_kraken_last_ts:  dict[str, str] = {}   # spot: "last" value from Kraken Trades API
_kraken_fut_ts:   dict[str, str] = {}   # perp: ISO timestamp of latest trade

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


def get_yes_velocity(yes_token: str, window_secs: float = 300.0) -> float | None:
    """Relative price change of the YES token over window_secs.

    Positive = YES rising (smart money buying YES = bullish).
    Negative = YES falling (selling pressure = bearish).
    Returns None when fewer than 5 history points are available.
    Only used as confirmation — never as standalone signal.
    """
    from . import ws_client as _ws
    history = _ws.get_token_price_history(yes_token, window_secs)
    if len(history) < 5:
        return None
    start, end = history[0][1], history[-1][1]
    if start <= 0:
        return None
    return round((end - start) / start, 4)


def get_yes_twap(yes_token: str, window_secs: float = 600.0) -> float | None:
    """Time-weighted average YES price over window_secs.

    Used as entry-quality check: buying significantly above TWAP = entering a
    micropump. Returns None when less than 30s of data is available.
    """
    from . import ws_client as _ws
    history = _ws.get_token_price_history(yes_token, window_secs)
    if len(history) < 3:
        return None
    weighted_sum = total_weight = 0.0
    for i in range(1, len(history)):
        dt = history[i][0] - history[i - 1][0]
        price = (history[i][1] + history[i - 1][1]) / 2
        weighted_sum += price * dt
        total_weight += dt
    if total_weight < 30.0:
        return None
    return round(weighted_sum / total_weight, 4)


def get_multi_coin_ofi_alignment(direction: str) -> int:
    """Count how many active coins have OFI aligned with direction ('UP' or 'DOWN').

    Does NOT encourage trading all coins simultaneously.
    """
    from .config_loader import CONFIG as _cfg
    coins = list(_cfg.get("coins", {}).keys())
    count = 0
    for coin in coins:
        ofi = get_order_flow_imbalance(coin)
        if ofi is None:
            continue
        if direction == "UP" and ofi > 0.55:
            count += 1
        elif direction == "DOWN" and ofi < 0.45:
            count += 1
    return count


def get_multi_coin_ofi_consensus(direction: str) -> tuple[int, int]:
    """Return (aligned, total_with_ofi) for coins with a valid OFI reading.

    Distinguishes unanimous confirmation from common correlated moves:
    - aligned == total → all measured coins agree → genuine macro signal
    - aligned < total/2 → majority of coins diverge → lower confidence

    Crypto coins are highly correlated (r>0.80), so 4/5 aligned is the
    normal state and not a meaningful boost. Only unanimity (5/5 or all
    coins with data) is an unusual enough event to warrant a score bump.
    """
    from .config_loader import CONFIG as _cfg
    coins = list(_cfg.get("coins", {}).keys())
    total = 0
    aligned = 0
    for coin in coins:
        ofi = get_order_flow_imbalance(coin)
        if ofi is None:
            continue
        total += 1
        if direction == "UP" and ofi > 0.55:
            aligned += 1
        elif direction == "DOWN" and ofi < 0.45:
            aligned += 1
    return aligned, total


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
    """Combine available signals into (direction, certainty 0–1).

    Direction: "UP", "DOWN", or None (no tradeable signal).
    Score: 0.0 = no signal, 1.0 = all signals aligned strongly.

    DATA-VALIDATED signal hierarchy (3970 closed triggered trades):
      OFI > 0.62 or < 0.38 → 74-75% directional accuracy  ← primary anchor
      OFI 0.55-0.62 / 0.38-0.45 → 50-54% (barely above random)
      OFI neutral 0.45-0.55 → 29% accuracy when other signals fire (HARMFUL)
      UNKNOWN regime → 44.8% accuracy (below random → penalised heavily)
      Funding rate → always neutral in all 3239 trades (removed)

    Hard gate: if OFI is neutral or unavailable → return (None, 0.0).
    Secondary signals: perp OFI, L/S ratio, liq proxy, depth, velocity.
    Removed: price_position (mean-reversion, correlated with OFI) and drift (same).
    """
    ofi = get_order_flow_imbalance(coin)

    # ── OFI hard gate ─────────────────────────────────────────────────────────
    # Data: neutral OFI (0.45-0.55) + other signals → 29% win rate (worse than random).
    # Other signals without an OFI anchor point the wrong direction 71% of the time.
    # No OFI data: primary signal missing → no conviction.
    if ofi is None or 0.45 <= ofi <= 0.55:
        return None, 0.0

    perp_ofi = get_perp_order_flow_imbalance(coin)
    ls = get_long_short_ratio(coin)
    liq = get_liquidation_proxy(coin)

    # Lazy import to avoid circular dependency at module level
    from . import regime as _regime

    bull_score = 0.0
    bear_score = 0.0

    # Spot OFI — primary signal (gate already passed, so ofi is outside 0.45-0.55)
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

    # Funding rate REMOVED — data shows FR_neutraal in 100% of 3239 trades.
    # An always-neutral signal contributes zero information; removed to reduce noise.

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

    # price_position (mean-reversion) and drift REMOVED from conviction:
    # Both measure the same recent price move as OFI but with delay — multicollinear.
    # Empirical: 0.70-0.85 scores (5/5 losses) were created by OFI + price_pos + drift
    # all pointing the same direction (same move, 3× measured). Removed 2026-05-29.

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

    # YES token price velocity — confirmation only; capped at 0.07 (correlated with OFI via price)
    if yes_token:
        velocity = get_yes_velocity(yes_token)
        if velocity is not None:
            if velocity > 0.02:
                bull_score += min(0.07, (velocity - 0.02) / 0.06 * 0.07)
            elif velocity < -0.02:
                bear_score += min(0.07, (-velocity - 0.02) / 0.06 * 0.07)

    # Market activity filter — thin market = signals are less reliable.
    if yes_token:
        from . import ws_client as _ws2
        if _ws2.get_market_activity(yes_token) < 5:
            bull_score *= 0.90
            bear_score *= 0.90

    # Regime multiplier
    # DATA: UNKNOWN regime → 44.8% directional accuracy (below random).
    # Multiplier 0.30 ensures score stays below any practical threshold in UNKNOWN.
    current_regime = _regime.get_current_regime(coin)
    multiplier = {
        "TRENDING": 0.85,
        "BREAKOUT": 0.90,
        "CHOPPY": 0.75,
        "RANGING": 1.15,
        "NORMAL": 1.0,
        "UNKNOWN": 0.30,  # data-validated: UNKNOWN regime is worse than random
    }.get(current_regime or "UNKNOWN", 0.30)
    bull_score *= multiplier
    bear_score *= multiplier

    max_score = max(bull_score, bear_score)
    if max_score < 0.15:
        return None, 0.0

    direction = "UP" if bull_score >= bear_score else "DOWN"
    return direction, round(min(1.0, max_score), 3)


# ── Kraken trade poller (Binance geo-blocked in DE/EU) ────────────────────────

_POLL_INTERVAL = 10.0  # seconds between polls


def _kraken_parse_spot(data: dict, coin: str) -> list:
    """Extract trades list from Kraken /Trades response."""
    result = data.get("result", {})
    trades = next((v for k, v in result.items() if k != "last"), [])
    return trades


async def refresh_ofi(coin: str) -> None:
    """Fetch the latest Kraken spot trades for one coin on-demand.

    Called directly before signal stamping so OFI is always fresh at entry time.
    """
    if coin not in _COIN_TO_KRAKEN:
        return
    pair = _COIN_TO_KRAKEN[coin]
    try:
        params: dict = {"pair": pair, "count": 500}
        last_ts = _kraken_last_ts.get(coin)
        if last_ts:
            params["since"] = last_ts
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{_KRAKEN_REST}/Trades", params=params)
            resp.raise_for_status()
            data = resp.json()
        trades = _kraken_parse_spot(data, coin)
        for t in trades:
            record_trade(coin, float(t[1]), t[3] == "b")
        last = data.get("result", {}).get("last")
        if last:
            _kraken_last_ts[coin] = str(last)
    except Exception as e:
        log.warning("ofi_refresh_failed", coin=coin, error=str(e))


async def run_trade_poll_loop() -> None:
    """Poll Kraken spot trades + Kraken Futures every 10s per coin for OFI.

    Binance is geo-blocked in DE/EU (HTTP 451 / CloudFront block).
    Kraken spot: GET /public/Trades?pair=XBTUSD&since=<nanotime>&count=1000
    Kraken Futures: GET /derivatives/api/v3/history?symbol=PF_XBTUSD&lastTime=<ISO>

    Kraken rate limit: ~1 req/s public — we add small sleeps between coins.
    """
    coins = [c for c in CONFIG.get("coins", {}).keys() if c in _COIN_TO_KRAKEN]
    log.info("trade_poll_loop_starting", coins=coins, source="kraken")

    async with httpx.AsyncClient(timeout=10) as client, \
               httpx.AsyncClient(timeout=10) as fut_client:

        # Seed spot — last 1000 trades per coin
        for coin in coins:
            pair = _COIN_TO_KRAKEN[coin]
            try:
                resp = await client.get(
                    f"{_KRAKEN_REST}/Trades",
                    params={"pair": pair, "count": 1000},
                )
                resp.raise_for_status()
                data = resp.json()
                trades = _kraken_parse_spot(data, coin)
                for t in trades:
                    record_trade(coin, float(t[1]), t[3] == "b")
                last = data.get("result", {}).get("last")
                if last:
                    _kraken_last_ts[coin] = str(last)
                log.info("trade_poll_seeded", coin=coin, n=len(trades), source="kraken")
            except Exception as e:
                log.warning("trade_poll_seed_failed", coin=coin, error=str(e))
            await asyncio.sleep(0.5)  # respect Kraken public rate limit

        # Seed perp — Kraken Futures history
        for coin in coins:
            fut_pair = _COIN_TO_KRAKEN_FUT.get(coin)
            if not fut_pair:
                continue
            try:
                resp = await fut_client.get(
                    f"{_KRAKEN_FUT}/history",
                    params={"symbol": fut_pair},
                )
                resp.raise_for_status()
                data = resp.json()
                trades = data.get("history", [])
                for t in trades:
                    record_perp_trade(coin, float(t.get("size", 0)), t.get("side") == "buy")
                if trades:
                    _kraken_fut_ts[coin] = trades[0].get("time", "")
                log.info("perp_trade_poll_seeded", coin=coin, n=len(trades), source="kraken_fut")
            except Exception as e:
                log.warning("perp_trade_poll_seed_failed", coin=coin, error=str(e))
            await asyncio.sleep(1.0)  # Kraken Futures: 1 req/s limit

        # Background polling loop
        while True:
            await asyncio.sleep(_POLL_INTERVAL)

            # Spot polling
            for coin in coins:
                pair = _COIN_TO_KRAKEN[coin]
                try:
                    params: dict = {"pair": pair, "count": 1000}
                    last_ts = _kraken_last_ts.get(coin)
                    if last_ts:
                        params["since"] = last_ts
                    resp = await client.get(f"{_KRAKEN_REST}/Trades", params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    trades = _kraken_parse_spot(data, coin)
                    for t in trades:
                        record_trade(coin, float(t[1]), t[3] == "b")
                    last = data.get("result", {}).get("last")
                    if last:
                        _kraken_last_ts[coin] = str(last)
                except Exception as e:
                    log.warning("trade_poll_failed", coin=coin, error=str(e))
                await asyncio.sleep(0.3)

            # Perp polling
            for coin in coins:
                fut_pair = _COIN_TO_KRAKEN_FUT.get(coin)
                if not fut_pair:
                    continue
                try:
                    params: dict = {"symbol": fut_pair}
                    last_time = _kraken_fut_ts.get(coin)
                    if last_time:
                        params["lastTime"] = last_time
                    resp = await fut_client.get(f"{_KRAKEN_FUT}/history", params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    trades = data.get("history", [])
                    for t in trades:
                        record_perp_trade(coin, float(t.get("size", 0)), t.get("side") == "buy")
                    if trades:
                        _kraken_fut_ts[coin] = trades[0].get("time", "")
                except Exception as e:
                    log.warning("perp_trade_poll_failed", coin=coin, error=str(e))
                await asyncio.sleep(0.5)


# ── Funding rate / L/S — skipped (Binance blocked in DE/EU) ───────────────────
# Funding rate was already removed from conviction (always neutral in 3239 trades).
# L/S ratio: Binance blocked, Bybit blocked (CloudFront). Skipping both.
# _funding_rates and _ls_ratios stay empty → conviction ignores them gracefully.

async def seed_funding_rates() -> None:
    log.info("funding_rate_seed_skipped", reason="binance_geo_blocked_DE_EU")


async def funding_rate_loop() -> None:
    log.info("funding_rate_loop_skipped", reason="binance_geo_blocked_DE_EU")
    while True:
        await asyncio.sleep(3600)  # sleep forever, no-op


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
