"""BTC Directional Indicator — minimaal floating venster.

Spot OFI: Kraken WebSocket (real-time, sub-seconde).
Perp OFI: Kraken Futures REST (elke 5s).
Regime:   Kraken OHLC REST (elke 2 min).

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

_KRAKEN_WS      = "wss://ws.kraken.com/v2"
_KRAKEN_REST    = "https://api.kraken.com/0/public"
_KRAKEN_FUT     = "https://futures.kraken.com/derivatives/api/v3"

_TRADE_WINDOW   = 300
_OFI_WINDOW     = 60
_MIN_TRADES     = 10

# ── Cache ──────────────────────────────────────────────────────────────────────

@dataclass
class _Cache:
    spot:           deque = field(default_factory=deque)
    perp:           deque = field(default_factory=deque)
    regime:         str   = "UNKNOWN"
    spot_ok:        bool  = False
    perp_ok:        bool  = False
    started:        bool  = False
    last_spot_ts:   float = 0.0   # when last spot trade arrived
    regime_fetched: float = 0.0
    kraken_fut_ts:  str   = ""
    lock:           threading.Lock = field(default_factory=threading.Lock)


@st.cache_resource
def _cache() -> _Cache:
    return _Cache()


# ── Signal computation ─────────────────────────────────────────────────────────

def _ofi(buf: deque, window: float) -> float | None:
    now    = time.time()
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


def _liq(buf: deque) -> float | None:
    now     = time.time()
    recent  = sum(q for ts, q, _ in buf if ts >= now - 30)
    pool    = [q for ts, q, _ in buf if now - _TRADE_WINDOW <= ts < now - 30]
    if not pool:
        return None
    baseline = sum(pool) / (min(270.0, _TRADE_WINDOW - 30) / 30)
    return recent / baseline if baseline > 1e-8 else None


_REGIME_MULT = {
    "RANGING": 1.15, "TRENDING": 0.85,
    "BREAKOUT": 0.90, "CHOPPY": 0.75,
    "NORMAL": 1.00, "UNKNOWN": 0.30,
}


def _direction(c: _Cache) -> tuple[str, float, dict]:
    with c.lock:
        spot_ofi = _ofi(c.spot, _OFI_WINDOW)
        perp_ofi = _ofi(c.perp, _OFI_WINDOW)
        liq_val  = _liq(c.spot)
        regime   = c.regime

    d = dict(spot_ofi=spot_ofi, perp_ofi=perp_ofi, liq=liq_val, regime=regime)

    if spot_ofi is None or 0.45 <= spot_ofi <= 0.55:
        return "undecided", 0.0, d

    bull = bear = 0.0

    if spot_ofi > 0.55:
        bull += (spot_ofi - 0.55) / 0.45
    else:
        bear += (0.45 - spot_ofi) / 0.45

    if perp_ofi is not None and not (0.45 <= perp_ofi <= 0.55):
        w = min(0.30, ((perp_ofi - 0.55) / 0.45 if perp_ofi > 0.55 else (0.45 - perp_ofi) / 0.45) * 0.50)
        if perp_ofi > 0.55:
            bull += w
        else:
            bear += w

    if liq_val is not None and liq_val > 2.0:
        bonus = min(0.10, (liq_val - 2.0) * 0.05)
        if bull >= bear:
            bull += bonus
        else:
            bear += bonus

    mult  = _REGIME_MULT.get(regime, 0.30)
    bull *= mult
    bear *= mult

    score = min(1.0, max(bull, bear))
    if score < 0.15:
        return "undecided", 0.0, d

    return ("up" if bull >= bear else "down"), round(score, 3), d


# ── Background streams ─────────────────────────────────────────────────────────

async def _spot_ws(c: _Cache) -> None:
    """Kraken WebSocket v2 — real-time BTC/USD trades."""
    import websockets
    subscribe = json.dumps({
        "method": "subscribe",
        "params": {"channel": "trade", "symbol": ["BTC/USD"]},
    })
    while True:
        try:
            async with websockets.connect(_KRAKEN_WS, ping_interval=20) as ws:
                await ws.send(subscribe)
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("channel") != "trade":
                        continue
                    now = time.time()
                    with c.lock:
                        for t in msg.get("data", []):
                            c.spot.append((now, float(t["qty"]), t["side"] == "buy"))
                        cutoff = now - _TRADE_WINDOW
                        while c.spot and c.spot[0][0] < cutoff:
                            c.spot.popleft()
                        c.spot_ok     = True
                        c.last_spot_ts = now
        except Exception:
            with c.lock:
                c.spot_ok = False
            await asyncio.sleep(3)


async def _perp_rest(c: _Cache) -> None:
    """Kraken Futures REST — BTC perp trades every 5s."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp   = await client.get(f"{_KRAKEN_FUT}/history", params={"symbol": "PF_XBTUSD"})
            trades = resp.json().get("history", [])
            now    = time.time()
            with c.lock:
                for t in trades:
                    c.perp.append((now, float(t.get("size", 0)), t.get("side") == "buy"))
                if trades:
                    c.kraken_fut_ts = trades[0].get("time", "")
                c.perp_ok = bool(trades)
        except Exception:
            pass

        while True:
            await asyncio.sleep(5)
            try:
                with c.lock:
                    last = c.kraken_fut_ts
                params: dict = {"symbol": "PF_XBTUSD"}
                if last:
                    params["lastTime"] = last
                resp   = await client.get(f"{_KRAKEN_FUT}/history", params=params)
                trades = resp.json().get("history", [])
                if trades:
                    now = time.time()
                    with c.lock:
                        for t in trades:
                            c.perp.append((now, float(t.get("size", 0)), t.get("side") == "buy"))
                        cutoff = now - _TRADE_WINDOW
                        while c.perp and c.perp[0][0] < cutoff:
                            c.perp.popleft()
                        c.kraken_fut_ts = trades[0].get("time", last)
                        c.perp_ok = True
            except Exception:
                with c.lock:
                    c.perp_ok = False


async def _regime_loop(c: _Cache) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                resp    = await client.get(_KRAKEN_REST + "/OHLC", params={"pair": "XBTUSD", "interval": 1})
                result  = resp.json().get("result", {})
                candles = next((v for k, v in result.items() if k != "last"), [])
                if len(candles) >= 10:
                    recent    = candles[-30:]
                    range_pct = (max(float(c[2]) for c in recent) - min(float(c[3]) for c in recent)) / float(recent[-1][4]) * 100
                    regime    = "RANGING" if range_pct < 0.40 else "TRENDING" if range_pct > 1.50 else "NORMAL"
                    with c.lock:
                        c.regime = regime
                    c.regime_fetched = time.time()
            except Exception:
                pass
            await asyncio.sleep(120)


def _run(c: _Cache) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(asyncio.gather(_spot_ws(c), _perp_rest(c), _regime_loop(c)))


def _start(c: _Cache) -> None:
    if not c.started:
        c.started = True
        threading.Thread(target=_run, args=(c,), daemon=True).start()


# ── UI ─────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="BTC", page_icon="📈", layout="centered")

st.markdown("""
<style>
  #MainMenu, header, footer, [data-testid="stDecoration"],
  [data-testid="stToolbar"] { display: none !important; }
  .block-container {
    padding: 0.4rem 0.5rem 0 !important;
    max-width: 210px !important;
    margin: 0 auto;
  }
</style>
""", unsafe_allow_html=True)

cache = _cache()
_start(cache)


@st.fragment(run_every=2)
def _panel() -> None:
    with cache.lock:
        n      = len(cache.spot)
        s_ok   = cache.spot_ok
        p_ok   = cache.perp_ok
        last_t = cache.last_spot_ts

    age = int(time.time() - last_t) if last_t else None

    if n < 50:
        st.markdown(f"""
<div style="background:#f9731618;border:2px solid #f97316;border-radius:10px;
            padding:12px 6px;text-align:center;font-family:system-ui;">
  <div style="font-size:32px;">⏳</div>
  <div style="font-size:14px;font-weight:700;color:#f97316;">Verbinden…</div>
  <div style="font-size:10px;color:#888;margin-top:4px;">{n} trades · Kraken WS</div>
</div>""", unsafe_allow_html=True)
        return

    direction, score, d = _direction(cache)

    with cache.lock:
        regime = cache.regime

    if direction == "up":
        color, label, arrow, emoji = "#22c55e", "STIJGING", "▲", "🟢"
    elif direction == "down":
        color, label, arrow, emoji = "#ef4444", "DALING",   "▼", "🔴"
    else:
        color, label, arrow, emoji = "#f97316", "UNDECIDED","◆", "🟠"

    regime_color = {"RANGING":"#22c55e","TRENDING":"#3b82f6","CHOPPY":"#ef4444"}.get(regime,"#888")

    ofi_s = d["spot_ofi"]
    age_str = f"{age}s" if age is not None and age < 60 else ("—" if age is None else f"{age}s")

    st.markdown(f"""
<div style="background:{color}18;border:2px solid {color};border-radius:10px;
            padding:10px 6px 8px;text-align:center;font-family:system-ui;">
  <div style="font-size:40px;line-height:1.1;">{emoji}</div>
  <div style="font-size:22px;font-weight:800;color:{color};margin-top:2px;">{arrow} {label}</div>
  <div style="margin-top:5px;">
    <span style="font-size:10px;color:{regime_color};font-weight:700;
                 background:{regime_color}22;padding:1px 6px;border-radius:4px;">{regime}</span>
    <span style="font-size:10px;color:#888;margin-left:4px;">{score:.2f}</span>
  </div>
  <div style="font-size:10px;color:#555;margin-top:5px;border-top:1px solid #333;padding-top:5px;">
    OFI {f"{ofi_s:.3f}" if ofi_s is not None else "—"}
    &nbsp;·&nbsp;
    {"🟢" if s_ok else "🔴"} <span style="font-size:9px;">⟳ {age_str}</span>
    &nbsp;{"🟢" if p_ok else "🟡"}
  </div>
</div>""", unsafe_allow_html=True)


_panel()
