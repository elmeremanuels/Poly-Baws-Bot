"""BTC Directional Indicator — met S/R niveaus en context-berichten.

Signalen (Kraken REST, geen WebSocket):
  OFI spot   35%  /Trades 60s
  OBI        28%  /Depth 25 niveaus
  Momentum   20%  VWAP-drift 45s
  Perp OFI   12%  Futures /history
  CVD slope   5%  acceleratie

S/R niveaus:
  Orderboek walls  — grote bid/ask clusters (live, < 1.5% van prijs)
  OHLC pivots      — dagelijkse H/L (gisteren + vandaag)
  Volume nodes     — meest verhandelde $100-buckets (uit trade buffer)

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

_KRAKEN_REST = "https://api.kraken.com/0/public"
_KRAKEN_FUT  = "https://futures.kraken.com/derivatives/api/v3"

_TRADE_WINDOW = 300
_OFI_WINDOW   = 60
_MOM_WINDOW   = 45
_MIN_TRADES   = 10
_POLL_SECS    = 3


# ── Cache ──────────────────────────────────────────────────────────────────────

@dataclass
class _Cache:
    spot:          deque = field(default_factory=deque)   # (ts, qty, is_buy)
    prices:        deque = field(default_factory=deque)   # (ts, price, qty)
    perp:          deque = field(default_factory=deque)   # (ts, qty, is_buy)
    bids:          dict  = field(default_factory=dict)    # price→qty (25 levels)
    asks:          dict  = field(default_factory=dict)
    current_price: float = 0.0
    ohlc_levels:   list  = field(default_factory=list)    # [(price, label)]
    regime:        str   = "UNKNOWN"
    spot_ok:       bool  = False
    perp_ok:       bool  = False
    book_ok:       bool  = False
    started:       bool  = False
    last_poll_ts:  float = 0.0
    lock:          threading.Lock = field(default_factory=threading.Lock)


@st.cache_resource
def _cache() -> _Cache:
    return _Cache()


# ── Signal computation ─────────────────────────────────────────────────────────

def _ofi(buf: deque, window: float) -> float | None:
    cutoff = time.time() - window
    buy = sell = 0.0; n = 0
    for ts, qty, is_buy in buf:
        if ts < cutoff: continue
        n += 1
        if is_buy: buy += qty
        else:      sell += qty
    total = buy + sell
    return buy / total if n >= _MIN_TRADES and total > 1e-8 else None


def _liq(buf: deque) -> float | None:
    now    = time.time()
    recent = sum(q for ts, q, _ in buf if ts >= now - 30)
    pool   = [q for ts, q, _ in buf if now - _TRADE_WINDOW <= ts < now - 30]
    if not pool: return None
    baseline = sum(pool) / (min(270.0, _TRADE_WINDOW - 30) / 30)
    return recent / baseline if baseline > 1e-8 else None


def _cvd_slope(buf: deque, window: float = 60.0) -> float | None:
    now = time.time(); cutoff = now - window; mid = cutoff + window / 2
    early = sum((q if b else -q) for ts, q, b in buf if cutoff <= ts < mid)
    late  = sum((q if b else -q) for ts, q, b in buf if ts >= mid)
    total = sum(abs(q) for ts, q, _ in buf if ts >= cutoff)
    return (late - early) / total if total > 1e-8 else None


def _obi(c: _Cache) -> float | None:
    with c.lock:
        if not c.bids or not c.asks: return None
        bid_vol = sum(c.bids[p] for p in sorted(c.bids, reverse=True)[:10])
        ask_vol = sum(c.asks[p] for p in sorted(c.asks)[:10])
    total = bid_vol + ask_vol
    return bid_vol / total if total > 0 else None


def _momentum(prices: deque, window: float = 45.0) -> float | None:
    now = time.time(); cutoff = now - window; mid = now - window / 2
    recent = [(p, q) for ts, p, q in prices if ts >= mid]
    early  = [(p, q) for ts, p, q in prices if cutoff <= ts < mid]
    if len(recent) < 3 or len(early) < 3: return None
    def vwap(lst):
        v = sum(q for _, q in lst)
        return sum(p * q for p, q in lst) / v if v > 1e-10 else 0.0
    r, e = vwap(recent), vwap(early)
    if e == 0: return None
    return max(-1.0, min(1.0, (r - e) / e * 100 / 0.05))


_REGIME_MULT = {
    "RANGING": 1.15, "TRENDING": 0.85, "BREAKOUT": 0.90,
    "CHOPPY":  0.75, "NORMAL":   1.00, "UNKNOWN":  0.30,
}


def _direction(c: _Cache) -> tuple[str, float, dict]:
    with c.lock:
        spot_ofi = _ofi(c.spot, _OFI_WINDOW)
        perp_ofi = _ofi(c.perp, _OFI_WINDOW)
        liq_val  = _liq(c.spot)
        cvd      = _cvd_slope(c.spot, _OFI_WINDOW)
        regime   = c.regime
        prices   = c.prices

    mom = _momentum(prices, _MOM_WINDOW)
    obi = _obi(c)
    d   = dict(spot_ofi=spot_ofi, perp_ofi=perp_ofi, obi=obi,
               cvd=cvd, mom=mom, liq=liq_val, regime=regime)

    if spot_ofi is None or 0.45 <= spot_ofi <= 0.55:
        return "undecided", 0.0, d

    bull = bear = 0.0

    if spot_ofi > 0.55: bull += (spot_ofi - 0.55) / 0.45
    else:               bear += (0.45 - spot_ofi) / 0.45

    if obi is not None and not (0.40 <= obi <= 0.60):
        raw = (obi - 0.60) / 0.40 if obi > 0.60 else (0.40 - obi) / 0.40
        w = min(0.30, raw * 0.55)
        if obi > 0.60: bull += w
        else:          bear += w

    if mom is not None and abs(mom) > 0.10:
        w = min(0.25, abs(mom) * 0.25)
        if mom > 0: bull += w
        else:       bear += w

    if perp_ofi is not None and not (0.45 <= perp_ofi <= 0.55):
        raw = (perp_ofi - 0.55) / 0.45 if perp_ofi > 0.55 else (0.45 - perp_ofi) / 0.45
        w = min(0.20, raw * 0.40)
        if perp_ofi > 0.55: bull += w
        else:                bear += w

    if cvd is not None and abs(cvd) > 0.05:
        w = min(0.08, abs(cvd) * 0.12)
        if cvd > 0: bull += w
        else:       bear += w

    if liq_val is not None and liq_val > 2.0:
        bonus = min(0.08, (liq_val - 2.0) * 0.04)
        if bull >= bear: bull += bonus
        else:            bear += bonus

    mult = _REGIME_MULT.get(regime, 0.30)
    bull *= mult; bear *= mult
    score = min(1.0, max(bull, bear))
    if score < 0.15:
        return "undecided", 0.0, d
    return ("up" if bull >= bear else "down"), round(score, 3), d


# ── Support / Resistance ───────────────────────────────────────────────────────

def _book_walls(c: _Cache, threshold: float = 1.8) -> tuple[list, list]:
    """Grote orderboek-clusters binnen 1.5% van huidige prijs."""
    with c.lock:
        price = c.current_price
        if not price or not c.bids or not c.asks:
            return [], []
        bids = {p: v for p, v in c.bids.items() if price * 0.985 <= p < price}
        asks = {p: v for p, v in c.asks.items() if price < p <= price * 1.015}

    def walls(levels: dict, desc: bool) -> list:
        if not levels: return []
        avg = sum(levels.values()) / len(levels)
        out = [{"price": p, "vol": v, "label": "wall", "src": "book"}
               for p, v in levels.items() if v >= avg * threshold]
        return sorted(out, key=lambda x: x["price"], reverse=desc)

    return walls(bids, True), walls(asks, False)


def _vpoc_levels(c: _Cache, bucket: float = 100.0, n: int = 2) -> list[dict]:
    """Meest verhandelde prijsniveaus uit de trade buffer."""
    with c.lock:
        pts = list(c.prices)
        price = c.current_price
    if not pts or not price: return []
    buckets: dict[float, float] = {}
    for ts, p, q in pts:
        b = round(p / bucket) * bucket
        buckets[b] = buckets.get(b, 0.0) + q
    top = sorted(buckets.items(), key=lambda x: x[1], reverse=True)[:n * 2]
    out = []
    for p, v in top:
        if abs(p - price) / price < 0.005: continue  # skip current price band
        out.append({"price": p, "vol": v, "label": "volume", "src": "vpoc"})
        if len(out) >= n: break
    return out


def _compile_levels(c: _Cache) -> tuple[float, list, list]:
    """Alle S/R bronnen samengevoegd en gesorteerd (nearest first)."""
    with c.lock:
        price = c.current_price
        ohlc  = list(c.ohlc_levels)

    if not price:
        return 0.0, [], []

    book_sup, book_res = _book_walls(c)
    vpoc = _vpoc_levels(c)

    supports    = list(book_sup)
    resistances = list(book_res)

    for p, label in ohlc:
        entry = {"price": p, "vol": 0, "label": label, "src": "ohlc"}
        if p < price: supports.append(entry)
        elif p > price: resistances.append(entry)

    for lvl in vpoc:
        if lvl["price"] < price: supports.append(lvl)
        elif lvl["price"] > price: resistances.append(lvl)

    # Sorteer en dedupliceer niveaus binnen $200 van elkaar
    supports    = sorted(supports,    key=lambda x: x["price"], reverse=True)
    resistances = sorted(resistances, key=lambda x: x["price"])

    def dedup(lst: list) -> list:
        out: list = []
        for lvl in lst:
            if not out or abs(lvl["price"] - out[-1]["price"]) > 200:
                out.append(lvl)
        return out

    return price, dedup(supports), dedup(resistances)


def _wall_message(price: float, supports: list, resistances: list,
                  direction: str) -> tuple[str, str | None]:
    """
    Geeft (bericht, kleur-override) terug.
    Kleur: None = gebruik richting-kleur, anders bijv. "#f59e0b" (oranje).
    """
    if not price: return "", None

    sup = supports[0]    if supports    else None
    res = resistances[0] if resistances else None

    sup_pct = ((price - sup["price"]) / price * 100) if sup else 999.0
    res_pct = ((res["price"] - price) / price * 100) if res else 999.0

    # Op het niveau (< 0.12%)
    if sup_pct < 0.12:
        if direction == "down":
            return f"🧱 Op support {sup['price']:,.0f} — kans op bounce!", "#f59e0b"
        return f"🧱 Support {sup['price']:,.0f} — bevestigt stijging", "#22c55e"

    if res_pct < 0.12:
        if direction == "up":
            return f"🧱 Op weerstand {res['price']:,.0f} — kan stagneren", "#f59e0b"
        return f"🧱 Weerstand {res['price']:,.0f} — bevestigt daling", "#ef4444"

    # Nadert (< 0.35%)
    if direction == "down" and sup_pct < 0.35:
        return f"⚠️ Nadert support {sup['price']:,.0f} (${price - sup['price']:,.0f})", "#f59e0b"

    if direction == "up" and res_pct < 0.35:
        return f"⚠️ Nadert weerstand {res['price']:,.0f} (${res['price'] - price:,.0f})", "#f59e0b"

    # Neutraal dicht bij niveau (< 0.35%, andere richting)
    if sup_pct < 0.35:
        return f"Support {sup['price']:,.0f} vlakbij (${price - sup['price']:,.0f})", None
    if res_pct < 0.35:
        return f"Weerstand {res['price']:,.0f} vlakbij (${res['price'] - price:,.0f})", None

    return "", None


def _levels_html(price: float, supports: list, resistances: list) -> str:
    """Compacte prijs-niveaus tabel: weerstand boven, support onder."""
    rows: list[str] = []

    def src_icon(s: dict) -> str:
        if s["src"] == "book":   return "🧱"
        if s["src"] == "vpoc":   return "📊"
        return "📍"

    # Tot 2 weerstanden (verste bovenaan)
    for lvl in reversed(resistances[:2]):
        pct = (lvl["price"] - price) / price * 100
        rows.append(
            f'<tr>'
            f'<td style="color:#ef4444;padding:1px 3px;">{src_icon(lvl)}</td>'
            f'<td style="color:#ef4444;font-family:monospace;">{lvl["price"]:,.0f}</td>'
            f'<td style="color:#555;font-size:9px;">{lvl["label"]}</td>'
            f'<td style="color:#ef4444;text-align:right;">+{pct:.1f}%</td>'
            f'</tr>'
        )

    # Huidige prijs
    rows.append(
        f'<tr style="border-top:1px solid #333;border-bottom:1px solid #333;">'
        f'<td style="color:#aaa;">▶</td>'
        f'<td style="color:#fff;font-weight:700;font-family:monospace;">{price:,.0f}</td>'
        f'<td style="color:#555;font-size:9px;">nu</td>'
        f'<td></td>'
        f'</tr>'
    )

    # Tot 2 supports (dichtstbijzijnde eerst)
    for lvl in supports[:2]:
        pct = (price - lvl["price"]) / price * 100
        rows.append(
            f'<tr>'
            f'<td style="color:#22c55e;padding:1px 3px;">{src_icon(lvl)}</td>'
            f'<td style="color:#22c55e;font-family:monospace;">{lvl["price"]:,.0f}</td>'
            f'<td style="color:#555;font-size:9px;">{lvl["label"]}</td>'
            f'<td style="color:#22c55e;text-align:right;">-{pct:.1f}%</td>'
            f'</tr>'
        )

    if not rows:
        return '<div style="font-size:9px;color:#555;text-align:center;">Niveaus laden…</div>'

    return (
        f'<div style="font-size:10px;margin-top:6px;border-top:1px solid #1e1e1e;padding-top:5px;">'
        f'<div style="color:#666;font-size:9px;margin-bottom:2px;">'
        f'🧱 wall &nbsp; 📊 volume &nbsp; 📍 dagpivot</div>'
        f'<table style="width:100%;border-collapse:collapse;">{"".join(rows)}</table>'
        f'</div>'
    )


# ── Background polling loop ────────────────────────────────────────────────────

async def _poll_loop(c: _Cache) -> None:
    last_since    = ""
    last_regime   = 0.0
    fut_ts        = ""
    perp_counter  = 0

    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            now = time.time()

            # ── Spot trades ──────────────────────────────────────────────────
            try:
                params: dict = {"pair": "XBTUSD"}
                if last_since: params["since"] = last_since
                r      = await client.get(_KRAKEN_REST + "/Trades", params=params)
                res    = r.json().get("result", {})
                trades = next((v for k, v in res.items() if k != "last"), [])
                ns     = str(res.get("last", ""))
                if trades:
                    if ns: last_since = ns
                    cutoff = now - _TRADE_WINDOW
                    with c.lock:
                        for t in trades:
                            ts = float(t[2]); price = float(t[0]); qty = float(t[1])
                            c.spot.append((ts, qty, t[3] == "b"))
                            c.prices.append((ts, price, qty))
                        while c.spot   and c.spot[0][0]   < cutoff: c.spot.popleft()
                        while c.prices and c.prices[0][0] < cutoff: c.prices.popleft()
                        if c.prices: c.current_price = c.prices[-1][1]
                        c.spot_ok = True; c.last_poll_ts = now
            except Exception:
                with c.lock: c.spot_ok = False

            # ── Order book (25 niveaus voor wall-detectie) ───────────────────
            try:
                r    = await client.get(_KRAKEN_REST + "/Depth",
                                        params={"pair": "XBTUSD", "count": 25})
                res  = r.json().get("result", {})
                book = next((v for k, v in res.items()), {})
                with c.lock:
                    c.bids    = {float(p): float(v) for p, v, *_ in book.get("bids", [])}
                    c.asks    = {float(p): float(v) for p, v, *_ in book.get("asks", [])}
                    c.book_ok = bool(c.bids and c.asks)
            except Exception:
                with c.lock: c.book_ok = False

            # ── Perp (elke 2e cyclus) ────────────────────────────────────────
            perp_counter += 1
            if perp_counter >= 2:
                perp_counter = 0
                try:
                    pf: dict = {"symbol": "PF_XBTUSD"}
                    if fut_ts: pf["lastTime"] = fut_ts
                    r  = await client.get(_KRAKEN_FUT + "/history", params=pf)
                    fl = r.json().get("history", [])
                    if fl:
                        cutoff = now - _TRADE_WINDOW
                        with c.lock:
                            for t in fl:
                                c.perp.append((now, float(t.get("size", 0)),
                                               t.get("side") == "buy"))
                            while c.perp and c.perp[0][0] < cutoff: c.perp.popleft()
                            c.perp_ok = True
                        fut_ts = fl[0].get("time", fut_ts)
                except Exception:
                    with c.lock: c.perp_ok = False

            # ── Regime + OHLC pivots (elke 2 min) ───────────────────────────
            if now - last_regime >= 120:
                last_regime = now
                # 1-minuut OHLC voor regime
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
                        regime = ("RANGING"  if range_pct < 0.40 else
                                  "TRENDING" if range_pct > 1.50 else "NORMAL")
                        with c.lock: c.regime = regime
                except Exception:
                    pass

                # Dagelijkse OHLC voor pivots (gisteren H/L + vandaag H/L)
                try:
                    r = await client.get(_KRAKEN_REST + "/OHLC",
                                         params={"pair": "XBTUSD", "interval": 1440})
                    res     = r.json().get("result", {})
                    candles = next((v for k, v in res.items() if k != "last"), [])
                    levels: list[tuple[float, str]] = []
                    if len(candles) >= 2:
                        # Gisteren (candles[-2])
                        prev = candles[-2]
                        levels += [
                            (float(prev[2]), "gist.H"),
                            (float(prev[3]), "gist.L"),
                        ]
                        # Vandaag (candles[-1], lopende kaars)
                        curr = candles[-1]
                        levels += [
                            (float(curr[2]), "dag H"),
                            (float(curr[3]), "dag L"),
                        ]
                    with c.lock:
                        c.ohlc_levels = levels
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


# ── Sound ──────────────────────────────────────────────────────────────────────

def _play(direction: str) -> None:
    if direction == "up":
        s = ("var ctx=new(window.AudioContext||window.webkitAudioContext)();"
             "var o1=ctx.createOscillator(),o2=ctx.createOscillator(),g=ctx.createGain();"
             "o1.connect(g);o2.connect(g);g.connect(ctx.destination);"
             "o1.frequency.value=660;o2.frequency.value=880;o1.type='sine';o2.type='sine';"
             "g.gain.setValueAtTime(0.25,ctx.currentTime);"
             "g.gain.exponentialRampToValueAtTime(0.001,ctx.currentTime+0.35);"
             "o1.start(ctx.currentTime);o1.stop(ctx.currentTime+0.15);"
             "o2.start(ctx.currentTime+0.12);o2.stop(ctx.currentTime+0.35);")
    else:
        s = ("var ctx=new(window.AudioContext||window.webkitAudioContext)();"
             "var o1=ctx.createOscillator(),o2=ctx.createOscillator(),g=ctx.createGain();"
             "o1.connect(g);o2.connect(g);g.connect(ctx.destination);"
             "o1.frequency.value=440;o2.frequency.value=330;o1.type='sine';o2.type='sine';"
             "g.gain.setValueAtTime(0.25,ctx.currentTime);"
             "g.gain.exponentialRampToValueAtTime(0.001,ctx.currentTime+0.35);"
             "o1.start(ctx.currentTime);o1.stop(ctx.currentTime+0.15);"
             "o2.start(ctx.currentTime+0.12);o2.stop(ctx.currentTime+0.35);")
    st.components.v1.html(f"<script>{s}</script>", height=0)


# ── UI ─────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="BTC", page_icon="📈", layout="centered")
st.markdown("""
<style>
  #MainMenu, header, footer, [data-testid="stDecoration"],
  [data-testid="stToolbar"] { display: none !important; }
  .block-container {
    padding: 0.4rem 0.5rem 0 !important;
    max-width: 270px !important;
    margin: 0 auto;
  }
  div[data-testid="stCheckbox"] label { font-size: 11px !important; }
</style>
""", unsafe_allow_html=True)

cache = _cache()
_start(cache)

if "sound_on"  not in st.session_state: st.session_state["sound_on"]  = True
if "prev_dir"  not in st.session_state: st.session_state["prev_dir"]  = "undecided"

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
        st.markdown(f"""
<div style="background:#f9731618;border:2px solid #f97316;border-radius:10px;
            padding:12px 6px;text-align:center;font-family:system-ui;">
  <div style="font-size:32px;">⏳</div>
  <div style="font-size:14px;font-weight:700;color:#f97316;">{"Ophalen…" if not last_t else "Verbinden…"}</div>
  <div style="font-size:10px;color:#888;margin-top:4px;">{n} trades</div>
</div>""", unsafe_allow_html=True)
        return

    direction, score, d = _direction(cache)
    price, supports, resistances = _compile_levels(cache)
    wall_msg, wall_color = _wall_message(price, supports, resistances, direction)

    with cache.lock:
        regime = cache.regime

    prev = st.session_state.get("prev_dir", "undecided")
    if st.session_state.get("sound_on") and direction != "undecided" and direction != prev:
        _play(direction)
    st.session_state["prev_dir"] = direction

    if direction == "up":
        base_color, label, arrow, emoji = "#22c55e", "STIJGING",  "▲", "🟢"
    elif direction == "down":
        base_color, label, arrow, emoji = "#ef4444", "DALING",    "▼", "🔴"
    else:
        base_color, label, arrow, emoji = "#f97316", "UNDECIDED", "◆", "🟠"

    # Kleur-override bij wall-waarschuwing
    color = wall_color if wall_color and wall_msg else base_color

    rc    = {"RANGING": "#22c55e", "TRENDING": "#3b82f6",
             "CHOPPY":  "#ef4444"}.get(regime, "#888")
    age_s = f"{age}s" if age is not None else "—"

    ofi_s = d["spot_ofi"]
    obi_s = d["obi"]
    cvd_s = d["cvd"]
    mom_s = d["mom"]

    def _bar(val: float | None, bull: bool) -> str:
        if val is None: return '<span style="color:#555">—</span>'
        frac = max(0.0, min(1.0, val)); filled = round(frac * 5)
        clr  = "#22c55e" if bull else "#ef4444"
        return f'<span style="color:{clr};font-family:monospace">{"█"*filled}{"░"*(5-filled)}</span>'

    mom_html = ("—" if mom_s is None else
                f'<span style="color:{"#22c55e" if mom_s>0 else "#ef4444"}">'
                f'{"▲" if mom_s>0 else "▼"} {abs(mom_s):.2f}</span>')

    # Wall-bericht (alleen tonen als niet leeg)
    wall_row = (
        f'<div style="font-size:10px;color:{color};font-weight:600;'
        f'background:{color}22;border-radius:4px;padding:2px 4px;margin:4px 0 2px;">'
        f'{wall_msg}</div>'
        if wall_msg else ""
    )

    levels_html = _levels_html(price, supports, resistances) if price else ""

    st.markdown(f"""
<div style="background:{color}18;border:2px solid {color};border-radius:10px;
            padding:10px 6px 8px;text-align:center;font-family:system-ui;">
  <div style="font-size:38px;line-height:1.1;">{emoji}</div>
  <div style="font-size:21px;font-weight:800;color:{color};margin-top:2px;">{arrow} {label}</div>
  <div style="margin-top:4px;">
    <span style="font-size:10px;color:{rc};font-weight:700;
                 background:{rc}22;padding:1px 6px;border-radius:4px;">{regime}</span>
    <span style="font-size:10px;color:#888;margin-left:4px;">score {score:.2f}</span>
  </div>
  {wall_row}
  <table style="width:100%;margin-top:4px;font-size:10px;color:#aaa;border-collapse:collapse;">
    <tr>
      <td style="text-align:left;color:#666;">OFI</td>
      <td style="text-align:right;">{_bar(ofi_s, True)}</td>
      <td style="text-align:right;color:#555;">{f"{ofi_s:.3f}" if ofi_s is not None else "—"}</td>
    </tr>
    <tr>
      <td style="text-align:left;color:#666;">OBI</td>
      <td style="text-align:right;">{_bar(obi_s, True)}</td>
      <td style="text-align:right;color:#555;">{f"{obi_s:.3f}" if obi_s is not None else "—"}</td>
    </tr>
    <tr>
      <td style="text-align:left;color:#666;">MOM</td>
      <td colspan="2" style="text-align:right;">{mom_html}</td>
    </tr>
    <tr>
      <td style="text-align:left;color:#666;">CVD</td>
      <td colspan="2" style="text-align:right;color:#555;">
        {f"{cvd_s:+.3f}" if cvd_s is not None else "—"}
      </td>
    </tr>
  </table>
  {levels_html}
  <div style="font-size:9px;color:#444;margin-top:4px;border-top:1px solid #2a2a2a;padding-top:4px;">
    {"🟢" if s_ok else "🔴"}OFI {"🟢" if b_ok else "🟡"}Book {"🟢" if p_ok else "🟡"}Perp
    <span style="float:right;">⟳{age_s}</span>
  </div>
</div>""", unsafe_allow_html=True)


_panel()
