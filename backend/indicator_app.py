"""Standalone BTC Directional Indicator — floats over your trading window.

Verbindt direct met Binance WebSocket (geen bot vereist).
Berekent OFI + perp-OFI + volume spike → GROEN / ORANJE / ROOD.

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

# ── Binance endpoints ──────────────────────────────────────────────────────────

_SPOT_WS   = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
_PERP_WS   = "wss://fstream.binance.com/ws/btcusdt@aggTrade"
_FR_URL    = "https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1"
_LS_URL    = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=5m&limit=1"

_TRADE_WINDOW = 300   # 5 min rolling buffer
_OFI_WINDOW   = 60    # 60s window for OFI calculation

# ── Shared in-process cache ────────────────────────────────────────────────────

@dataclass
class _Cache:
    spot:        deque = field(default_factory=deque)  # (ts, qty, is_buy)
    perp:        deque = field(default_factory=deque)
    funding:     float | None = None
    ls_ratio:    float | None = None
    spot_ok:     bool = False
    perp_ok:     bool = False
    started:     bool = False
    fr_fetched:  float = 0.0
    ls_fetched:  float = 0.0
    lock:        threading.Lock = field(default_factory=threading.Lock)


@st.cache_resource
def _cache() -> _Cache:
    return _Cache()


# ── OFI + liq helpers ─────────────────────────────────────────────────────────

def _ofi(buf: deque, window: float) -> float | None:
    now = time.time()
    cutoff = now - window
    buy = sell = 0.0
    for ts, qty, is_buy in buf:
        if ts < cutoff:
            continue
        if is_buy:
            buy += qty
        else:
            sell += qty
    total = buy + sell
    return buy / total if total > 0.01 else None


def _liq_proxy(buf: deque) -> float | None:
    now = time.time()
    recent = sum(q for ts, q, _ in buf if ts >= now - 30)
    base_secs = min(270, now - _TRADE_WINDOW)  # avoid div-by-zero on startup
    if base_secs < 30:
        return None
    baseline = sum(q for ts, q, _ in buf if now - _TRADE_WINDOW <= ts < now - 30) / (base_secs / 30)
    return recent / baseline if baseline > 0.01 else None


# ── Direction computation ──────────────────────────────────────────────────────

def _direction(c: _Cache) -> tuple[str, float, dict]:
    """Returns ('up'|'down'|'undecided', score 0-1, detail dict)."""
    with c.lock:
        spot_ofi  = _ofi(c.spot, _OFI_WINDOW)
        perp_ofi  = _ofi(c.perp, _OFI_WINDOW)
        liq       = _liq_proxy(c.spot)
        fr        = c.funding
        ls        = c.ls_ratio

    detail = dict(spot_ofi=spot_ofi, perp_ofi=perp_ofi, liq=liq, fr=fr, ls=ls)

    # Hard gate: neutral OFI → no signal (same threshold as the bot)
    if spot_ofi is None or 0.45 <= spot_ofi <= 0.55:
        return "undecided", 0.0, detail

    bull = bear = 0.0

    # Spot OFI — primary signal
    if spot_ofi > 0.55:
        bull += (spot_ofi - 0.55) / 0.45
    else:
        bear += (0.45 - spot_ofi) / 0.45

    # Perp OFI — 40% weight secondary
    if perp_ofi is not None and not (0.45 <= perp_ofi <= 0.55):
        w = (perp_ofi - 0.55) / 0.45 if perp_ofi > 0.55 else (0.45 - perp_ofi) / 0.45
        if perp_ofi > 0.55:
            bull += w * 0.4
        else:
            bear += w * 0.4

    # Funding rate — high positive = crowded longs = mean-reversion DOWN
    if fr is not None:
        if fr > 0.001:
            bear += min(0.20, (fr - 0.001) / 0.005)
        elif fr < -0.001:
            bull += min(0.20, (-fr - 0.001) / 0.005)

    # Long/short ratio — crowded longs = L/S high = DOWN pressure
    if ls is not None:
        if ls > 1.5:
            bear += min(0.15, (ls - 1.5) * 0.1)
        elif ls < 0.7:
            bull += min(0.15, (0.7 - ls) * 0.1)

    # Volume spike bonus confirms dominant side
    if liq is not None and liq > 2.0:
        bonus = min(0.10, (liq - 2.0) * 0.05)
        if bull >= bear:
            bull += bonus
        else:
            bear += bonus

    score = min(1.0, max(bull, bear))
    if score < 0.05:
        return "undecided", 0.0, detail

    return ("up" if bull >= bear else "down"), score, detail


# ── Background asyncio loop ────────────────────────────────────────────────────

async def _spot_loop(c: _Cache) -> None:
    import websockets
    while True:
        try:
            async with websockets.connect(_SPOT_WS, ping_interval=20) as ws:
                with c.lock:
                    c.spot_ok = True
                async for raw in ws:
                    msg = json.loads(raw)
                    ts  = time.time()
                    qty = float(msg["q"])
                    is_buy = not msg["m"]   # m=True → taker is seller
                    with c.lock:
                        c.spot.append((ts, qty, is_buy))
                        cutoff = ts - _TRADE_WINDOW
                        while c.spot and c.spot[0][0] < cutoff:
                            c.spot.popleft()
        except Exception:
            with c.lock:
                c.spot_ok = False
            await asyncio.sleep(3)


async def _perp_loop(c: _Cache) -> None:
    import websockets
    while True:
        try:
            async with websockets.connect(_PERP_WS, ping_interval=20) as ws:
                with c.lock:
                    c.perp_ok = True
                async for raw in ws:
                    msg = json.loads(raw)
                    ts  = time.time()
                    qty = float(msg["q"])
                    is_buy = not msg["m"]
                    with c.lock:
                        c.perp.append((ts, qty, is_buy))
                        cutoff = ts - _TRADE_WINDOW
                        while c.perp and c.perp[0][0] < cutoff:
                            c.perp.popleft()
        except Exception:
            with c.lock:
                c.perp_ok = False
            await asyncio.sleep(3)


async def _rest_loop(c: _Cache) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            now = time.time()
            if now - c.fr_fetched > 300:
                try:
                    r = await client.get(_FR_URL)
                    data = r.json()
                    if data:
                        with c.lock:
                            c.funding = float(data[0]["fundingRate"])
                        c.fr_fetched = now
                except Exception:
                    pass
            if now - c.ls_fetched > 300:
                try:
                    r = await client.get(_LS_URL)
                    data = r.json()
                    if data:
                        with c.lock:
                            c.ls_ratio = float(data[0]["longShortRatio"])
                        c.ls_fetched = now
                except Exception:
                    pass
            await asyncio.sleep(60)


def _run_background(c: _Cache) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(asyncio.gather(
        _spot_loop(c),
        _perp_loop(c),
        _rest_loop(c),
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

# Strip Streamlit chrome for a clean floating window
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
    direction, score, d = _direction(cache)

    if direction == "up":
        color, label, emoji, arrow = "#22c55e", "STIJGING",  "🟢", "▲"
    elif direction == "down":
        color, label, emoji, arrow = "#ef4444", "DALING",    "🔴", "▼"
    else:
        color, label, emoji, arrow = "#f97316", "UNDECIDED", "🟠", "◆"

    spot_ofi  = d["spot_ofi"]
    perp_ofi  = d["perp_ofi"]
    liq       = d["liq"]
    fr        = d["fr"]
    ls        = d["ls"]

    # Big coloured indicator
    st.markdown(f"""
<div style="
  background:{color}18;
  border:3px solid {color};
  border-radius:14px;
  padding:16px 12px 12px;
  text-align:center;
  margin-bottom:6px;
">
  <div style="font-size:52px;line-height:1;">{emoji}</div>
  <div style="font-size:28px;font-weight:700;color:{color};margin-top:4px;">{arrow} {label}</div>
  <div style="font-size:12px;color:#888;margin-top:6px;">Score {score:.2f} &nbsp;·&nbsp; verversing 5s</div>
</div>
""", unsafe_allow_html=True)

    # Signal detail row
    c1, c2, c3, c4 = st.columns(4)

    def _fmt(v: float | None, fmt: str, suffix: str = "") -> str:
        return f"{v:{fmt}}{suffix}" if v is not None else "—"

    c1.metric("OFI spot",   _fmt(spot_ofi,  ".3f"))
    c2.metric("OFI perp",   _fmt(perp_ofi,  ".3f"))
    c3.metric("Vol spike",  _fmt(liq,        ".1f", "×"))
    c4.metric("L/S ratio",  _fmt(ls,         ".2f"))

    # Connection status line
    with cache.lock:
        s_ok = cache.spot_ok
        p_ok = cache.perp_ok

    now = time.time()
    with cache.lock:
        n_spot = len(cache.spot)

    warmup = n_spot < 50
    if warmup:
        st.caption(f"⏳ Verbinden & opwarmen… ({n_spot} trades ontvangen)")
    else:
        st.caption(
            f"{'🟢' if s_ok else '🔴'} Spot &nbsp;·&nbsp; "
            f"{'🟢' if p_ok else '🔴'} Perp"
            + (f"&nbsp;·&nbsp; FR {fr * 100:.4f}%" if fr is not None else "")
        )


_panel()
