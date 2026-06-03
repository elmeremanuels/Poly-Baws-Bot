"""BTC Directional Indicator — minimaal floating venster.

Signalen en gewichten (onderzoek: Cont et al. 2014, Hasbrouck 1991):
  OFI spot   — 35% — /Trades, 60s window (primair, meest robuust)
  OBI        — 28% — /Depth, top-10 orderboek (sterkst < 1 min)
  Momentum   — 20% — VWAP-drift 45s (prijs onafhankelijk van volume)
  Perp OFI   — 12% — Futures /history (futures leidt spot-discovery)
  CVD slope  —  5% — versnelling koopdruk (bevestiging)

Alle via Kraken REST polling, geen WebSocket nodig.

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

_KRAKEN_REST  = "https://api.kraken.com/0/public"
_KRAKEN_FUT   = "https://futures.kraken.com/derivatives/api/v3"

_TRADE_WINDOW = 300   # seconds to keep in buffer
_OFI_WINDOW   = 60    # seconds for OFI / CVD
_MOM_WINDOW   = 45    # seconds for price momentum
_MIN_TRADES   = 10    # minimum trades needed for OFI
_POLL_SECS    = 3     # main REST poll interval


# ── Cache ──────────────────────────────────────────────────────────────────────

@dataclass
class _Cache:
    # (timestamp, qty, is_buy) — voor OFI / CVD / liq
    spot:   deque = field(default_factory=deque)
    # (timestamp, price, qty) — voor VWAP momentum
    prices: deque = field(default_factory=deque)
    # (timestamp, qty, is_buy) — futures OFI
    perp:   deque = field(default_factory=deque)
    # orderboek voor OBI
    bids:   dict  = field(default_factory=dict)
    asks:   dict  = field(default_factory=dict)

    regime:       str   = "UNKNOWN"
    spot_ok:      bool  = False
    perp_ok:      bool  = False
    book_ok:      bool  = False
    started:      bool  = False
    last_poll_ts: float = 0.0
    lock:         threading.Lock = field(default_factory=threading.Lock)


@st.cache_resource
def _cache() -> _Cache:
    return _Cache()


# ── Signal computation ─────────────────────────────────────────────────────────

def _ofi(buf: deque, window: float) -> float | None:
    """Buy / (buy + sell) volume over window. None als te weinig data."""
    cutoff = time.time() - window
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
    return buy / total if n >= _MIN_TRADES and total > 1e-8 else None


def _liq(buf: deque) -> float | None:
    """Volume spike: recente 30s t.o.v. voorgaande baseline."""
    now     = time.time()
    recent  = sum(q for ts, q, _ in buf if ts >= now - 30)
    pool    = [q for ts, q, _ in buf if now - _TRADE_WINDOW <= ts < now - 30]
    if not pool:
        return None
    baseline = sum(pool) / (min(270.0, _TRADE_WINDOW - 30) / 30)
    return recent / baseline if baseline > 1e-8 else None


def _cvd_slope(buf: deque, window: float = 60.0) -> float | None:
    """CVD versnelling: late helft vs vroege helft van window. Genormaliseerd op totaal volume."""
    now    = time.time()
    cutoff = now - window
    mid    = cutoff + window / 2
    early  = sum((q if b else -q) for ts, q, b in buf if cutoff <= ts < mid)
    late   = sum((q if b else -q) for ts, q, b in buf if ts >= mid)
    total  = sum(abs(q) for ts, q, _ in buf if ts >= cutoff)
    return (late - early) / total if total > 1e-8 else None


def _obi(c: _Cache) -> float | None:
    """Order Book Imbalance: top-10 bid-volume / (bid + ask).
    > 0.60 = koopdruk; < 0.40 = verkoopdruk."""
    with c.lock:
        if not c.bids or not c.asks:
            return None
        top_bids = sorted(c.bids.keys(), reverse=True)[:10]
        top_asks = sorted(c.asks.keys())[:10]
        bid_vol  = sum(c.bids[p] for p in top_bids)
        ask_vol  = sum(c.asks[p] for p in top_asks)
    total = bid_vol + ask_vol
    return bid_vol / total if total > 0 else None


def _momentum(prices: deque, window: float = 45.0) -> float | None:
    """VWAP-drift: recente helft vs vroege helft van 45s window.
    Returns -1..+1; schaal: 0.05% prijsbeweging = score 1.0."""
    now    = time.time()
    cutoff = now - window
    mid    = now - window / 2

    recent = [(p, q) for ts, p, q in prices if ts >= mid]
    early  = [(p, q) for ts, p, q in prices if cutoff <= ts < mid]

    if len(recent) < 3 or len(early) < 3:
        return None

    def vwap(lst: list) -> float:
        v = sum(q for _, q in lst)
        return sum(p * q for p, q in lst) / v if v > 1e-10 else 0.0

    r, e = vwap(recent), vwap(early)
    if e == 0:
        return None

    # 0.05% = full signal (1.0); groter wordt geclipt op 1.0
    return max(-1.0, min(1.0, (r - e) / e * 100 / 0.05))


_REGIME_MULT = {
    "RANGING":  1.15,   # meest voorspelbaar
    "TRENDING": 0.85,   # overshoot-risico
    "BREAKOUT": 0.90,
    "CHOPPY":   0.75,   # laagste voorspelbaarheid
    "NORMAL":   1.00,
    "UNKNOWN":  0.30,
}


def _direction(c: _Cache) -> tuple[str, float, dict]:
    """Berekent richting + score + debug-dict."""
    with c.lock:
        spot_ofi = _ofi(c.spot, _OFI_WINDOW)
        perp_ofi = _ofi(c.perp, _OFI_WINDOW)
        liq_val  = _liq(c.spot)
        cvd      = _cvd_slope(c.spot, _OFI_WINDOW)
        regime   = c.regime
        prices   = c.prices   # reference; lock still held

    mom = _momentum(prices, _MOM_WINDOW)   # uses own sub-lock via deque
    obi = _obi(c)

    d = dict(spot_ofi=spot_ofi, perp_ofi=perp_ofi, obi=obi,
             cvd=cvd, mom=mom, liq=liq_val, regime=regime)

    # OFI is de gate: neutraal = geen signaal
    if spot_ofi is None or 0.45 <= spot_ofi <= 0.55:
        return "undecided", 0.0, d

    bull = bear = 0.0

    # ── OFI spot — 35% van composite (max ~1.0) ──────────────────────────────
    if spot_ofi > 0.55:
        bull += (spot_ofi - 0.55) / 0.45
    else:
        bear += (0.45 - spot_ofi) / 0.45

    # ── OBI — 28%, cap 0.30 ───────────────────────────────────────────────────
    if obi is not None and not (0.40 <= obi <= 0.60):
        raw = (obi - 0.60) / 0.40 if obi > 0.60 else (0.40 - obi) / 0.40
        w   = min(0.30, raw * 0.55)
        if obi > 0.60:
            bull += w
        else:
            bear += w

    # ── Momentum (VWAP 45s) — 20%, cap 0.25 ──────────────────────────────────
    if mom is not None and abs(mom) > 0.10:
        w = min(0.25, abs(mom) * 0.25)
        if mom > 0:
            bull += w
        else:
            bear += w

    # ── Perp OFI — 12%, cap 0.20 ──────────────────────────────────────────────
    if perp_ofi is not None and not (0.45 <= perp_ofi <= 0.55):
        raw = (perp_ofi - 0.55) / 0.45 if perp_ofi > 0.55 else (0.45 - perp_ofi) / 0.45
        w   = min(0.20, raw * 0.40)
        if perp_ofi > 0.55:
            bull += w
        else:
            bear += w

    # ── CVD slope — 5%, cap 0.08 ──────────────────────────────────────────────
    if cvd is not None and abs(cvd) > 0.05:
        w = min(0.08, abs(cvd) * 0.12)
        if cvd > 0:
            bull += w
        else:
            bear += w

    # ── Volume spike — bonus, max 0.08 ────────────────────────────────────────
    if liq_val is not None and liq_val > 2.0:
        bonus = min(0.08, (liq_val - 2.0) * 0.04)
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


# ── Background polling loop ────────────────────────────────────────────────────

async def _poll_loop(c: _Cache) -> None:
    last_trade_since = ""
    last_regime_ts   = 0.0
    kraken_fut_ts    = ""
    perp_counter     = 0

    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            now = time.time()

            # ── Spot trades + prices ─────────────────────────────────────────
            try:
                params: dict = {"pair": "XBTUSD"}
                if last_trade_since:
                    params["since"] = last_trade_since
                r      = await client.get(_KRAKEN_REST + "/Trades", params=params)
                res    = r.json().get("result", {})
                trades = next((v for k, v in res.items() if k != "last"), [])
                new_since = str(res.get("last", ""))
                if trades:
                    if new_since:
                        last_trade_since = new_since
                    cutoff = now - _TRADE_WINDOW
                    with c.lock:
                        for t in trades:
                            # Kraken: [price, vol, time_float, "b"/"s", ...]
                            ts    = float(t[2])
                            price = float(t[0])
                            qty   = float(t[1])
                            c.spot.append((ts, qty, t[3] == "b"))
                            c.prices.append((ts, price, qty))
                        while c.spot   and c.spot[0][0]   < cutoff: c.spot.popleft()
                        while c.prices and c.prices[0][0] < cutoff: c.prices.popleft()
                        c.spot_ok    = True
                        c.last_poll_ts = now
            except Exception:
                with c.lock:
                    c.spot_ok = False

            # ── Order book (OBI) ─────────────────────────────────────────────
            try:
                r    = await client.get(_KRAKEN_REST + "/Depth",
                                        params={"pair": "XBTUSD", "count": 10})
                res  = r.json().get("result", {})
                book = next((v for k, v in res.items()), {})
                with c.lock:
                    # Kraken depth: [[price_str, vol_str, ts], ...]
                    c.bids    = {float(p): float(v) for p, v, *_ in book.get("bids", [])}
                    c.asks    = {float(p): float(v) for p, v, *_ in book.get("asks", [])}
                    c.book_ok = bool(c.bids and c.asks)
            except Exception:
                with c.lock:
                    c.book_ok = False

            # ── Perp trades (elke 2e cyclus ≈ 6s) ───────────────────────────
            perp_counter += 1
            if perp_counter >= 2:
                perp_counter = 0
                try:
                    params_fut: dict = {"symbol": "PF_XBTUSD"}
                    if kraken_fut_ts:
                        params_fut["lastTime"] = kraken_fut_ts
                    r        = await client.get(_KRAKEN_FUT + "/history",
                                                params=params_fut)
                    fut_list = r.json().get("history", [])
                    if fut_list:
                        cutoff = now - _TRADE_WINDOW
                        with c.lock:
                            for t in fut_list:
                                c.perp.append((now, float(t.get("size", 0)),
                                               t.get("side") == "buy"))
                            while c.perp and c.perp[0][0] < cutoff:
                                c.perp.popleft()
                            c.perp_ok = True
                        kraken_fut_ts = fut_list[0].get("time", kraken_fut_ts)
                except Exception:
                    with c.lock:
                        c.perp_ok = False

            # ── Regime (elke 2 min) ──────────────────────────────────────────
            if now - last_regime_ts >= 120:
                try:
                    r = await client.get(_KRAKEN_REST + "/OHLC",
                                         params={"pair": "XBTUSD", "interval": 1})
                    res     = r.json().get("result", {})
                    candles = next((v for k, v in res.items() if k != "last"), [])
                    if len(candles) >= 10:
                        recent    = candles[-30:]
                        range_pct = (
                            max(float(k[2]) for k in recent) -
                            min(float(k[3]) for k in recent)
                        ) / float(recent[-1][4]) * 100
                        regime = (
                            "RANGING"  if range_pct < 0.40 else
                            "TRENDING" if range_pct > 1.50 else
                            "NORMAL"
                        )
                        with c.lock:
                            c.regime = regime
                        last_regime_ts = now
                except Exception:
                    pass

            await asyncio.sleep(_POLL_SECS)


def _run(c: _Cache) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_poll_loop(c))


def _start(c: _Cache) -> None:
    if not c.started:
        c.started = True
        threading.Thread(target=_run, args=(c,), daemon=True).start()


# ── Sound helper ───────────────────────────────────────────────────────────────

def _play(direction: str) -> None:
    if direction == "up":
        script = (
            "var ctx=new(window.AudioContext||window.webkitAudioContext)();"
            "var o1=ctx.createOscillator(),o2=ctx.createOscillator(),g=ctx.createGain();"
            "o1.connect(g);o2.connect(g);g.connect(ctx.destination);"
            "o1.frequency.value=660;o2.frequency.value=880;"
            "o1.type='sine';o2.type='sine';"
            "g.gain.setValueAtTime(0.25,ctx.currentTime);"
            "g.gain.exponentialRampToValueAtTime(0.001,ctx.currentTime+0.35);"
            "o1.start(ctx.currentTime);o1.stop(ctx.currentTime+0.15);"
            "o2.start(ctx.currentTime+0.12);o2.stop(ctx.currentTime+0.35);"
        )
    else:
        script = (
            "var ctx=new(window.AudioContext||window.webkitAudioContext)();"
            "var o1=ctx.createOscillator(),o2=ctx.createOscillator(),g=ctx.createGain();"
            "o1.connect(g);o2.connect(g);g.connect(ctx.destination);"
            "o1.frequency.value=440;o2.frequency.value=330;"
            "o1.type='sine';o2.type='sine';"
            "g.gain.setValueAtTime(0.25,ctx.currentTime);"
            "g.gain.exponentialRampToValueAtTime(0.001,ctx.currentTime+0.35);"
            "o1.start(ctx.currentTime);o1.stop(ctx.currentTime+0.15);"
            "o2.start(ctx.currentTime+0.12);o2.stop(ctx.currentTime+0.35);"
        )
    st.components.v1.html(f"<script>{script}</script>", height=0)


# ── UI ─────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="BTC", page_icon="📈", layout="centered")

st.markdown("""
<style>
  #MainMenu, header, footer, [data-testid="stDecoration"],
  [data-testid="stToolbar"] { display: none !important; }
  .block-container {
    padding: 0.4rem 0.5rem 0 !important;
    max-width: 230px !important;
    margin: 0 auto;
  }
  div[data-testid="stCheckbox"] label { font-size: 11px !important; }
</style>
""", unsafe_allow_html=True)

cache = _cache()
_start(cache)

if "sound_on" not in st.session_state:
    st.session_state["sound_on"] = True
if "prev_dir" not in st.session_state:
    st.session_state["prev_dir"] = "undecided"

sound_on = st.checkbox("🔊 Geluid", value=st.session_state["sound_on"], key="sound_cb")
st.session_state["sound_on"] = sound_on


@st.fragment(run_every=2)
def _panel() -> None:
    with cache.lock:
        n      = len(cache.spot)
        s_ok   = cache.spot_ok
        p_ok   = cache.perp_ok
        b_ok   = cache.book_ok
        last_t = cache.last_poll_ts

    age = int(time.time() - last_t) if last_t else None

    if not s_ok or n < 20:
        status = "Ophalen…" if not last_t else "Verbinden…"
        st.markdown(f"""
<div style="background:#f9731618;border:2px solid #f97316;border-radius:10px;
            padding:12px 6px;text-align:center;font-family:system-ui;">
  <div style="font-size:32px;">⏳</div>
  <div style="font-size:14px;font-weight:700;color:#f97316;">{status}</div>
  <div style="font-size:10px;color:#888;margin-top:4px;">{n} trades</div>
</div>""", unsafe_allow_html=True)
        return

    direction, score, d = _direction(cache)

    with cache.lock:
        regime = cache.regime

    prev = st.session_state.get("prev_dir", "undecided")
    if (st.session_state.get("sound_on") and
            direction != "undecided" and direction != prev):
        _play(direction)
    st.session_state["prev_dir"] = direction

    if direction == "up":
        color, label, arrow, emoji = "#22c55e", "STIJGING", "▲", "🟢"
    elif direction == "down":
        color, label, arrow, emoji = "#ef4444", "DALING",   "▼", "🔴"
    else:
        color, label, arrow, emoji = "#f97316", "UNDECIDED", "◆", "🟠"

    rc    = {"RANGING": "#22c55e", "TRENDING": "#3b82f6",
             "CHOPPY":  "#ef4444"}.get(regime, "#888")
    age_s = f"{age}s" if age is not None else "—"

    ofi_s = d["spot_ofi"]
    obi_s = d["obi"]
    cvd_s = d["cvd"]
    mom_s = d["mom"]

    def _bar(val: float | None, lo: float, hi: float, up_is_bull: bool) -> str:
        if val is None:
            return '<span style="color:#555">—</span>'
        frac   = max(0.0, min(1.0, (val - lo) / (hi - lo)))
        filled = round(frac * 5)
        clr    = "#22c55e" if (up_is_bull and val > (lo + hi) / 2) else "#ef4444"
        return f'<span style="color:{clr};font-family:monospace">{"█"*filled}{"░"*(5-filled)}</span>'

    ofi_bar = _bar(ofi_s, 0.0, 1.0, True)
    obi_bar = _bar(obi_s, 0.0, 1.0, True)

    # Momentum: toon als pijl + waarde
    if mom_s is None:
        mom_html = '<span style="color:#555">—</span>'
    else:
        mc = "#22c55e" if mom_s > 0 else "#ef4444"
        ma = "▲" if mom_s > 0 else "▼"
        mom_html = f'<span style="color:{mc}">{ma} {abs(mom_s):.2f}</span>'

    st.markdown(f"""
<div style="background:{color}18;border:2px solid {color};border-radius:10px;
            padding:10px 6px 8px;text-align:center;font-family:system-ui;">
  <div style="font-size:40px;line-height:1.1;">{emoji}</div>
  <div style="font-size:22px;font-weight:800;color:{color};margin-top:2px;">{arrow} {label}</div>
  <div style="margin-top:5px;">
    <span style="font-size:10px;color:{rc};font-weight:700;
                 background:{rc}22;padding:1px 6px;border-radius:4px;">{regime}</span>
    <span style="font-size:10px;color:#888;margin-left:4px;">score {score:.2f}</span>
  </div>
  <table style="width:100%;margin-top:6px;font-size:10px;color:#aaa;border-collapse:collapse;">
    <tr>
      <td style="text-align:left;padding:1px 2px;color:#666;">OFI</td>
      <td style="text-align:right;">{ofi_bar}</td>
      <td style="text-align:right;color:#555;">{f"{ofi_s:.3f}" if ofi_s is not None else "—"}</td>
    </tr>
    <tr>
      <td style="text-align:left;padding:1px 2px;color:#666;">OBI</td>
      <td style="text-align:right;">{obi_bar}</td>
      <td style="text-align:right;color:#555;">{f"{obi_s:.3f}" if obi_s is not None else "—"}</td>
    </tr>
    <tr>
      <td style="text-align:left;padding:1px 2px;color:#666;">MOM</td>
      <td colspan="2" style="text-align:right;">{mom_html}</td>
    </tr>
    <tr>
      <td style="text-align:left;padding:1px 2px;color:#666;">CVD</td>
      <td colspan="2" style="text-align:right;color:#555;">
        {f"{cvd_s:+.3f}" if cvd_s is not None else "—"}
      </td>
    </tr>
  </table>
  <div style="font-size:9px;color:#444;margin-top:5px;border-top:1px solid #2a2a2a;padding-top:4px;">
    {"🟢" if s_ok else "🔴"}OFI {"🟢" if b_ok else "🟡"}OBI {"🟢" if p_ok else "🟡"}Perp
    <span style="float:right;">⟳{age_s}</span>
  </div>
</div>""", unsafe_allow_html=True)


_panel()
