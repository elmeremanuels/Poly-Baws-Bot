"""Standalone BTC Directional Indicator — floats over your trading window.

Gebruikt Kraken spot + Kraken Futures (Bybit/Binance geblokkeerd in DE/EU).
Berekent OFI + regime → GROEN / ORANJE / ROOD elke 5 seconden.

Usage:
    streamlit run backend/indicator_app.py --server.port 8502 --server.headless true
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import httpx
import streamlit as st

# ── Kraken endpoints (werkt vanuit DE/EU) ─────────────────────────────────────

_KRAKEN_TRADES   = "https://api.kraken.com/0/public/Trades"
_KRAKEN_OHLC     = "https://api.kraken.com/0/public/OHLC"
_KRAKEN_FUT_HIST = "https://futures.kraken.com/derivatives/api/v3/history"

_TRADE_WINDOW = 300   # 5 min rolling buffer
_OFI_WINDOW   = 60    # 60s OFI
_MIN_TRADES   = 10
_POLL_SECS    = 5

# ── Cache ──────────────────────────────────────────────────────────────────────

@dataclass
class _Cache:
    spot:          deque = field(default_factory=deque)   # (ts, qty, is_buy)
    perp:          deque = field(default_factory=deque)
    regime:        str = "UNKNOWN"
    spot_ok:       bool = False
    perp_ok:       bool = False
    started:       bool = False
    kraken_since:  str = "0"
    kraken_fut_ts: str = ""
    regime_fetched: float = 0.0
    lock:          threading.Lock = field(default_factory=threading.Lock)


@st.cache_resource
def _cache() -> _Cache:
    return _Cache()


# ── OFI + liq helpers ─────────────────────────────────────────────────────────

def _ofi(buf: deque, window: float) -> float | None:
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
    now = time.time()
    recent = sum(q for ts, q, _ in buf if ts >= now - 30)
    baseline_pool = [q for ts, q, _ in buf if now - _TRADE_WINDOW <= ts < now - 30]
    if not baseline_pool:
        return None
    baseline = sum(baseline_pool) / (min(270.0, _TRADE_WINDOW - 30) / 30)
    return recent / baseline if baseline > 1e-8 else None


# ── Regime via Kraken 1m klines ────────────────────────────────────────────────

async def _fetch_regime(client: httpx.AsyncClient) -> str:
    try:
        resp = await client.get(_KRAKEN_OHLC, params={"pair": "XBTUSD", "interval": 1})
        resp.raise_for_status()
        candles = resp.json().get("result", {}).get("XXBTZUSD", [])
        if len(candles) < 10:
            return "UNKNOWN"
        recent = candles[-30:]
        highs  = [float(c[2]) for c in recent]
        lows   = [float(c[3]) for c in recent]
        closes = [float(c[4]) for c in recent]
        range_pct = (max(highs) - min(lows)) / closes[-1] * 100
        if range_pct < 0.40:
            return "RANGING"
        if range_pct > 1.50:
            return "TRENDING"
        return "NORMAL"
    except Exception:
        return "UNKNOWN"


# ── Direction (zelfde gewichten als de bot) ────────────────────────────────────

_REGIME_MULT = {
    "RANGING":  1.15,
    "TRENDING": 0.85,
    "BREAKOUT": 0.90,
    "CHOPPY":   0.75,
    "NORMAL":   1.00,
    "UNKNOWN":  0.30,
}


def _direction(c: _Cache) -> tuple[str, float, dict]:
    with c.lock:
        spot_ofi = _ofi(c.spot, _OFI_WINDOW)
        perp_ofi = _ofi(c.perp, _OFI_WINDOW)
        liq      = _liq_proxy(c.spot)
        regime   = c.regime

    detail = dict(spot_ofi=spot_ofi, perp_ofi=perp_ofi, liq=liq, regime=regime)

    if spot_ofi is None or 0.45 <= spot_ofi <= 0.55:
        return "undecided", 0.0, detail

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

    if liq is not None and liq > 2.0:
        bonus = min(0.10, (liq - 2.0) * 0.05)
        if bull >= bear:
            bull += bonus
        else:
            bear += bonus

    mult = _REGIME_MULT.get(regime, 0.30)
    bull *= mult
    bear *= mult

    score = min(1.0, max(bull, bear))
    if score < 0.15:
        return "undecided", 0.0, detail

    return ("up" if bull >= bear else "down"), round(score, 3), detail


# ── Kraken spot polling ────────────────────────────────────────────────────────

async def _kraken_spot_loop(c: _Cache) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        # Seed: laatste 1000 trades
        try:
            resp = await client.get(_KRAKEN_TRADES, params={"pair": "XBTUSD", "count": 1000})
            data = resp.json()
            trades = data.get("result", {}).get("XXBTZUSD", [])
            now = time.time()
            with c.lock:
                for t in trades:
                    # [price, volume, time, buy_sell, market_limit, misc, tradeId]
                    c.spot.append((now, float(t[1]), t[3] == "b"))
                c.kraken_since = str(data.get("result", {}).get("last", "0"))
                c.spot_ok = bool(trades)
        except Exception:
            pass

        while True:
            await asyncio.sleep(_POLL_SECS)
            try:
                with c.lock:
                    since = c.kraken_since
                resp = await client.get(
                    _KRAKEN_TRADES,
                    params={"pair": "XBTUSD", "since": since, "count": 1000},
                )
                data = resp.json()
                trades = data.get("result", {}).get("XXBTZUSD", [])
                now = time.time()
                with c.lock:
                    for t in trades:
                        c.spot.append((now, float(t[1]), t[3] == "b"))
                    cutoff = now - _TRADE_WINDOW
                    while c.spot and c.spot[0][0] < cutoff:
                        c.spot.popleft()
                    if trades:
                        c.kraken_since = str(data.get("result", {}).get("last", since))
                    c.spot_ok = True
            except Exception:
                with c.lock:
                    c.spot_ok = False


# ── Kraken Futures polling (perp OFI) ─────────────────────────────────────────

async def _kraken_fut_loop(c: _Cache) -> None:
    """Kraken Futures BTC/USD perp — voor perp OFI als secundair signaal."""
    async with httpx.AsyncClient(timeout=10) as client:
        # Seed
        try:
            resp = await client.get(_KRAKEN_FUT_HIST, params={"symbol": "PF_XBTUSD"})
            data = resp.json()
            trades = data.get("history", [])
            now = time.time()
            with c.lock:
                for t in trades:
                    # {uid, time, price, size, side: "buy"/"sell", type}
                    qty    = float(t.get("size", 0))
                    is_buy = t.get("side") == "buy"
                    c.perp.append((now, qty, is_buy))
                c.kraken_fut_ts = trades[0].get("time", "") if trades else ""
                c.perp_ok = bool(trades)
        except Exception:
            pass

        while True:
            await asyncio.sleep(_POLL_SECS)
            try:
                with c.lock:
                    last_ts = c.kraken_fut_ts
                params: dict = {"symbol": "PF_XBTUSD"}
                if last_ts:
                    params["lastTime"] = last_ts
                resp = await client.get(_KRAKEN_FUT_HIST, params=params)
                data = resp.json()
                trades = data.get("history", [])
                if trades:
                    now = time.time()
                    with c.lock:
                        for t in trades:
                            qty    = float(t.get("size", 0))
                            is_buy = t.get("side") == "buy"
                            c.perp.append((now, qty, is_buy))
                        cutoff = now - _TRADE_WINDOW
                        while c.perp and c.perp[0][0] < cutoff:
                            c.perp.popleft()
                        c.kraken_fut_ts = trades[0].get("time", last_ts)
                        c.perp_ok = True
            except Exception:
                with c.lock:
                    c.perp_ok = False


async def _regime_loop(c: _Cache) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        regime = await _fetch_regime(client)
        with c.lock:
            c.regime = regime
        c.regime_fetched = time.time()

        while True:
            await asyncio.sleep(120)
            regime = await _fetch_regime(client)
            with c.lock:
                c.regime = regime
            c.regime_fetched = time.time()


def _run_background(c: _Cache) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(asyncio.gather(
        _kraken_spot_loop(c),
        _kraken_fut_loop(c),
        _regime_loop(c),
    ))


def _ensure_running(c: _Cache) -> None:
    if not c.started:
        c.started = True
        threading.Thread(target=_run_background, args=(c,), daemon=True).start()


# ── Streamlit UI ───────────────────────────────────────────────────────────────

st.set_page_config(page_title="BTC Indicator", page_icon="📈", layout="centered")

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

    if n_spot < 50:
        st.markdown(f"""
<div style="background:#f9731618;border:3px solid #f97316;border-radius:14px;
            padding:20px 12px;text-align:center;">
  <div style="font-size:40px;">⏳</div>
  <div style="font-size:22px;font-weight:700;color:#f97316;margin-top:6px;">Opwarmen…</div>
  <div style="font-size:12px;color:#888;margin-top:6px;">
    Kraken spot {"✓" if s_ok else "verbinden…"} · {n_spot} trades ontvangen
  </div>
</div>""", unsafe_allow_html=True)
        return

    direction, score, d = _direction(cache)

    if direction == "up":
        color, label, arrow, emoji = "#22c55e", "STIJGING",  "▲", "🟢"
    elif direction == "down":
        color, label, arrow, emoji = "#ef4444", "DALING",    "▼", "🔴"
    else:
        color, label, arrow, emoji = "#f97316", "UNDECIDED", "◆", "🟠"

    regime_color = {
        "RANGING": "#22c55e", "TRENDING": "#3b82f6",
        "BREAKOUT": "#8b5cf6", "CHOPPY": "#ef4444",
    }.get(regime, "#888")

    spot_ofi = d["spot_ofi"]
    perp_ofi = d["perp_ofi"]
    liq      = d["liq"]

    st.markdown(f"""
<div style="background:{color}18;border:3px solid {color};border-radius:14px;
            padding:16px 12px 10px;text-align:center;margin-bottom:8px;">
  <div style="font-size:52px;line-height:1;">{emoji}</div>
  <div style="font-size:28px;font-weight:700;color:{color};margin-top:4px;">{arrow} {label}</div>
  <div style="margin-top:8px;">
    <span style="font-size:12px;color:{regime_color};font-weight:600;
                 background:{regime_color}22;padding:2px 8px;border-radius:6px;">{regime}</span>
    &nbsp;<span style="font-size:12px;color:#888;">score {score:.2f}</span>
  </div>
</div>""", unsafe_allow_html=True)

    def _f(v: float | None, fmt: str, sfx: str = "") -> str:
        return f"{v:{fmt}}{sfx}" if v is not None else "—"

    c1, c2, c3 = st.columns(3)
    c1.metric("OFI spot",  _f(spot_ofi, ".3f"))
    c2.metric("OFI perp",  _f(perp_ofi, ".3f"))
    c3.metric("Vol spike", _f(liq,       ".1f", "×"))

    st.caption(
        f"{'🟢' if s_ok else '🔴'} Kraken spot · "
        f"{'🟢' if p_ok else '🟡'} Kraken fut · "
        f"verversing 5s"
    )


_panel()
