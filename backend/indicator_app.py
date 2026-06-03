"""Standalone BTC Directional Indicator — floats over your trading window.

Verbindt via Binance REST (WebSocket geblokkeerd op datacenter-IPs).
Berekent precies dezelfde signalen als de bot, inclusief regime-multiplier.

Usage:
    streamlit run backend/indicator_app.py --server.port 8502 --server.headless true
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import httpx
import streamlit as st

# ── Binance REST endpoints ─────────────────────────────────────────────────────

_SPOT_BASE  = "https://api.binance.com/api/v3"
_PERP_BASE  = "https://fapi.binance.com"
_SYMBOL     = "BTCUSDT"
_POLL_SECS  = 5    # aggTrade poll interval
_REST_TIMEOUT = 10

_TRADE_WINDOW = 300   # 5 min rolling buffer
_OFI_WINDOW   = 60    # 60s OFI (matches bot)
_MIN_TRADES   = 10    # minimum trades in window for reliable OFI (matches bot)

# ── Shared in-process cache ────────────────────────────────────────────────────

@dataclass
class _Cache:
    spot:         deque = field(default_factory=deque)   # (ts, qty, is_buy)
    perp:         deque = field(default_factory=deque)
    ls_ratio:     float | None = None
    regime:       str = "UNKNOWN"
    spot_ok:      bool = False
    perp_ok:      bool = False
    started:      bool = False
    regime_fetched: float = 0.0
    ls_fetched:   float = 0.0
    last_spot_id: int = 0
    last_perp_id: int = 0
    lock:         threading.Lock = field(default_factory=threading.Lock)


@st.cache_resource
def _cache() -> _Cache:
    return _Cache()


# ── OFI + liq helpers ─────────────────────────────────────────────────────────

def _ofi(buf: deque, window: float) -> float | None:
    """buy_volume / total_volume for the last `window` seconds."""
    now = time.time()
    cutoff = now - window
    buy = sell = 0.0
    n = 0
    for ts, qty, is_buy in buf:
        if ts < cutoff:
            continue
        n += 1
        if is_buy:
            buy += qty
        else:
            sell += qty
    total = buy + sell
    if n < _MIN_TRADES or total < 1e-8:
        return None
    return buy / total


def _liq_proxy(buf: deque) -> float | None:
    """Recent-30s volume / baseline-30s volume. > 2 = spike."""
    now = time.time()
    recent = sum(q for ts, q, _ in buf if ts >= now - 30)
    baseline_secs = min(270.0, max(30.0, now - (now - _TRADE_WINDOW)))
    baseline_pool = [q for ts, q, _ in buf if now - _TRADE_WINDOW <= ts < now - 30]
    if not baseline_pool:
        return None
    baseline = sum(baseline_pool) / (baseline_secs / 30)
    return recent / baseline if baseline > 1e-8 else None


# ── Regime detection via 1m klines ────────────────────────────────────────────
# Mirrors bot's primary regime check (regime.py:96-99):
#   range_pct < 0.4%  → RANGING  (signals most reliable, multiplier 1.15×)
#   range_pct > 1.5%  → TRENDING (multiplier 0.85×)
#   else              → NORMAL   (multiplier 1.0×)
# UNKNOWN is the startup state (multiplier 0.30×) until klines are fetched.

async def _fetch_regime(client: httpx.AsyncClient) -> str:
    try:
        resp = await client.get(
            f"{_SPOT_BASE}/klines",
            params={"symbol": _SYMBOL, "interval": "1m", "limit": 30},
        )
        resp.raise_for_status()
        klines = resp.json()
        if not klines:
            return "UNKNOWN"
        highs  = [float(k[2]) for k in klines]
        lows   = [float(k[3]) for k in klines]
        closes = [float(k[4]) for k in klines]
        range_abs = max(highs) - min(lows)
        range_pct = range_abs / closes[-1] * 100
        if range_pct < 0.40:
            return "RANGING"
        if range_pct > 1.50:
            return "TRENDING"
        return "NORMAL"
    except Exception:
        return "UNKNOWN"


# ── Direction computation (matches bot exactly) ────────────────────────────────

_REGIME_MULT = {
    "RANGING":  1.15,
    "BREAKOUT": 0.90,
    "TRENDING": 0.85,
    "CHOPPY":   0.75,
    "NORMAL":   1.00,
    "UNKNOWN":  0.30,   # data: UNKNOWN → 44.8% accuracy, worse than random
}

def _direction(c: _Cache) -> tuple[str, float, dict]:
    """Returns ('up'|'down'|'undecided', score 0-1, detail dict)."""
    with c.lock:
        spot_ofi = _ofi(c.spot, _OFI_WINDOW)
        perp_ofi = _ofi(c.perp, _OFI_WINDOW)
        liq      = _liq_proxy(c.spot)
        ls       = c.ls_ratio
        regime   = c.regime

    detail = dict(spot_ofi=spot_ofi, perp_ofi=perp_ofi, liq=liq, ls=ls, regime=regime)

    # Hard gate: neutral or missing OFI → no signal (same as bot)
    if spot_ofi is None or 0.45 <= spot_ofi <= 0.55:
        return "undecided", 0.0, detail

    bull = bear = 0.0

    # Spot OFI — primary
    if spot_ofi > 0.55:
        bull += (spot_ofi - 0.55) / 0.45
    else:
        bear += (0.45 - spot_ofi) / 0.45

    # Perp OFI — 0.50× secondary (futures lead spot)
    if perp_ofi is not None and not (0.45 <= perp_ofi <= 0.55):
        w = (perp_ofi - 0.55) / 0.45 if perp_ofi > 0.55 else (0.45 - perp_ofi) / 0.45
        w = min(0.30, w * 0.50)
        if perp_ofi > 0.55:
            bull += w
        else:
            bear += w

    # Long/Short ratio — contrarian: crowded side reverts
    # Bot thresholds: > 1.5 bearish, < 0.67 bullish
    if ls is not None:
        if ls > 1.5:
            bear += min(0.15, (ls - 1.5) / 1.5)
        elif ls < 0.67:
            bull += min(0.15, (0.67 - ls) / 0.67)

    # Volume spike — confirms dominant side
    if liq is not None and liq > 2.0:
        bonus = min(0.10, (liq - 2.0) * 0.05)
        if bull >= bear:
            bull += bonus
        else:
            bear += bonus

    # Regime multiplier (the single biggest accuracy driver)
    mult = _REGIME_MULT.get(regime, 0.30)
    bull *= mult
    bear *= mult

    # Score threshold — matches bot (< 0.15 → no trade)
    score = min(1.0, max(bull, bear))
    if score < 0.15:
        return "undecided", 0.0, detail

    return ("up" if bull >= bear else "down"), round(score, 3), detail


# ── Background REST polling ────────────────────────────────────────────────────

async def _spot_loop(c: _Cache) -> None:
    """Poll Binance spot aggTrades every 5s. Mirrors bot's run_trade_poll_loop()."""
    async with httpx.AsyncClient(timeout=_REST_TIMEOUT) as client:
        # Seed: last 500 trades to pre-fill OFI buffer
        try:
            resp = await client.get(
                f"{_SPOT_BASE}/aggTrades",
                params={"symbol": _SYMBOL, "limit": 500},
            )
            trades = resp.json()
            now = time.time()
            with c.lock:
                for t in trades:
                    c.spot.append((now, float(t["q"]), not bool(t["m"])))
                if trades:
                    c.last_spot_id = int(trades[-1]["a"])
                c.spot_ok = True
        except Exception:
            pass

        while True:
            await asyncio.sleep(_POLL_SECS)
            try:
                params: dict = {"symbol": _SYMBOL, "limit": 500}
                with c.lock:
                    last = c.last_spot_id
                if last:
                    params["fromId"] = last + 1
                resp = await client.get(f"{_SPOT_BASE}/aggTrades", params=params)
                trades = resp.json()
                if trades and isinstance(trades, list):
                    now = time.time()
                    with c.lock:
                        for t in trades:
                            c.spot.append((now, float(t["q"]), not bool(t["m"])))
                        cutoff = now - _TRADE_WINDOW
                        while c.spot and c.spot[0][0] < cutoff:
                            c.spot.popleft()
                        c.last_spot_id = int(trades[-1]["a"])
                        c.spot_ok = True
            except Exception:
                with c.lock:
                    c.spot_ok = False


async def _perp_loop(c: _Cache) -> None:
    """Poll Binance perp aggTrades every 5s."""
    async with httpx.AsyncClient(timeout=_REST_TIMEOUT) as client:
        try:
            resp = await client.get(
                f"{_PERP_BASE}/fapi/v1/aggTrades",
                params={"symbol": _SYMBOL, "limit": 500},
            )
            trades = resp.json()
            now = time.time()
            with c.lock:
                for t in trades:
                    c.perp.append((now, float(t["q"]), not bool(t["m"])))
                if trades:
                    c.last_perp_id = int(trades[-1]["a"])
                c.perp_ok = True
        except Exception:
            pass

        while True:
            await asyncio.sleep(_POLL_SECS)
            try:
                params: dict = {"symbol": _SYMBOL, "limit": 500}
                with c.lock:
                    last = c.last_perp_id
                if last:
                    params["fromId"] = last + 1
                resp = await client.get(f"{_PERP_BASE}/fapi/v1/aggTrades", params=params)
                trades = resp.json()
                if trades and isinstance(trades, list):
                    now = time.time()
                    with c.lock:
                        for t in trades:
                            c.perp.append((now, float(t["q"]), not bool(t["m"])))
                        cutoff = now - _TRADE_WINDOW
                        while c.perp and c.perp[0][0] < cutoff:
                            c.perp.popleft()
                        c.last_perp_id = int(trades[-1]["a"])
                        c.perp_ok = True
            except Exception:
                with c.lock:
                    c.perp_ok = False


async def _rest_poller(c: _Cache) -> None:
    """Poll L/S ratio + regime (klines) every 5 minutes."""
    async with httpx.AsyncClient(timeout=_REST_TIMEOUT) as client:
        # Fetch regime immediately on startup
        regime = await _fetch_regime(client)
        with c.lock:
            c.regime = regime
        c.regime_fetched = time.time()

        while True:
            await asyncio.sleep(60)
            now = time.time()

            if now - c.ls_fetched >= 300:
                try:
                    resp = await client.get(
                        f"{_PERP_BASE}/futures/data/globalLongShortAccountRatio",
                        params={"symbol": _SYMBOL, "period": "5m", "limit": 1},
                    )
                    data = resp.json()
                    if data:
                        with c.lock:
                            c.ls_ratio = float(data[0]["longShortRatio"])
                    c.ls_fetched = now
                except Exception:
                    pass

            if now - c.regime_fetched >= 120:
                regime = await _fetch_regime(client)
                with c.lock:
                    c.regime = regime
                c.regime_fetched = now


def _run_background(c: _Cache) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(asyncio.gather(
        _spot_loop(c),
        _perp_loop(c),
        _rest_poller(c),
    ))


def _ensure_running(c: _Cache) -> None:
    if not c.started:
        c.started = True
        t = threading.Thread(target=_run_background, args=(c,), daemon=True)
        t.start()


# ── Streamlit UI ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="BTC Indicator",
    page_icon="📈",
    layout="centered",
)

st.markdown("""
<style>
  #MainMenu, header, footer { display: none !important; }
  .block-container { padding: 0.5rem 1rem 0 !important; }
  [data-testid="stDecoration"] { display: none !important; }
</style>
""", unsafe_allow_html=True)

cache = _cache()
_ensure_running(cache)


@st.fragment(run_every=5)
def _panel() -> None:
    with cache.lock:
        n_spot = len(cache.spot)
        s_ok   = cache.spot_ok
        p_ok   = cache.perp_ok
        regime = cache.regime
        ls     = cache.ls_ratio

    warmup = n_spot < 50

    if warmup:
        st.markdown("""
<div style="
  background:#f9731618;
  border:3px solid #f97316;
  border-radius:14px;
  padding:16px 12px 12px;
  text-align:center;
">
  <div style="font-size:40px;">⏳</div>
  <div style="font-size:22px;font-weight:700;color:#f97316;margin-top:4px;">Opwarmen…</div>
  <div style="font-size:12px;color:#888;margin-top:6px;">Trades ontvangen: {n}</div>
</div>
""".format(n=n_spot), unsafe_allow_html=True)
        return

    direction, score, d = _direction(cache)

    if direction == "up":
        color, label, arrow, emoji = "#22c55e", "STIJGING",  "▲", "🟢"
    elif direction == "down":
        color, label, arrow, emoji = "#ef4444", "DALING",    "▼", "🔴"
    else:
        color, label, arrow, emoji = "#f97316", "UNDECIDED", "◆", "🟠"

    spot_ofi = d["spot_ofi"]
    perp_ofi = d["perp_ofi"]
    liq      = d["liq"]

    # Regime colour
    regime_color = {
        "RANGING": "#22c55e", "TRENDING": "#3b82f6", "BREAKOUT": "#8b5cf6",
        "CHOPPY": "#ef4444", "NORMAL": "#888", "UNKNOWN": "#888",
    }.get(regime, "#888")

    st.markdown(f"""
<div style="
  background:{color}18;
  border:3px solid {color};
  border-radius:14px;
  padding:16px 12px 10px;
  text-align:center;
  margin-bottom:8px;
">
  <div style="font-size:52px;line-height:1;">{emoji}</div>
  <div style="font-size:28px;font-weight:700;color:{color};margin-top:4px;">{arrow} {label}</div>
  <div style="margin-top:8px;">
    <span style="font-size:12px;color:{regime_color};font-weight:600;
                 background:{regime_color}22;padding:2px 8px;border-radius:6px;">
      {regime}
    </span>
    &nbsp;
    <span style="font-size:12px;color:#888;">score {score:.2f}</span>
  </div>
</div>
""", unsafe_allow_html=True)

    def _fmt(v: float | None, fmt: str, suffix: str = "") -> str:
        return f"{v:{fmt}}{suffix}" if v is not None else "—"

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("OFI spot",   _fmt(spot_ofi, ".3f"))
    c2.metric("OFI perp",   _fmt(perp_ofi, ".3f"))
    c3.metric("Vol spike",  _fmt(liq,       ".1f", "×"))
    c4.metric("L/S ratio",  _fmt(ls,        ".2f"))

    conn = f"{'🟢' if s_ok else '🔴'} Spot REST · {'🟢' if p_ok else '🔴'} Perp REST"
    st.caption(f"{conn} · verversing 5s")


_panel()
