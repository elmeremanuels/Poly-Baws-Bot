"""Shared indicator engine — exact signal computation from indicator_app.py.

Used by:
  indicator_app.py  — Streamlit dashboard on port 8502
  stoplicht_signals.py — scalper trading signal

Each process runs its own independent polling thread and cache.
Data format is identical across both consumers.

Signals:
  OFI  spot    35 %   /Trades 60s
  OBI          28 %   /Depth  25 niveaus
  Momentum     20 %   VWAP-drift 45s
  Perp OFI     12 %   Futures /history
  CVD slope     5 %   acceleratie
  Liq bonus     var   volume spike confirmation
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

_KRAKEN_REST = "https://api.kraken.com/0/public"
_KRAKEN_FUT  = "https://futures.kraken.com/derivatives/api/v3"

_TRADE_WINDOW = 300   # seconds of history to keep
_OFI_WINDOW   = 60
_MOM_WINDOW   = 45
_MIN_TRADES   = 10
_POLL_SECS    = 3

_COIN_TO_SPOT_PAIR: dict[str, str] = {
    "BTC":  "XBTUSD",
    "ETH":  "ETHUSD",
    "SOL":  "SOLUSD",
    "XRP":  "XRPUSD",
    "DOGE": "XDGEUSD",
}

_COIN_TO_PERP_PAIR: dict[str, str] = {
    "BTC":  "PF_XBTUSD",
    "ETH":  "PF_ETHUSD",
    "SOL":  "PF_SOLUSD",
    "XRP":  "PF_XRPUSD",
    "DOGE": "PF_DOGEUSD",
}

_REGIME_MULT: dict[str, float] = {
    "RANGING": 1.15, "TRENDING": 0.85, "BREAKOUT": 0.90,
    "CHOPPY":  0.75, "NORMAL":   1.00, "UNKNOWN":  0.30,
}


# ── Cache ──────────────────────────────────────────────────────────────────────

@dataclass
class Cache:
    coin:          str   = "BTC"
    spot:          deque = field(default_factory=deque)   # (ts, qty, is_buy)
    prices:        deque = field(default_factory=deque)   # (ts, price, qty)
    perp:          deque = field(default_factory=deque)   # (ts, qty, is_buy)
    bids:          dict  = field(default_factory=dict)    # price → qty (25 levels)
    asks:          dict  = field(default_factory=dict)
    current_price: float = 0.0
    ohlc_levels:   list  = field(default_factory=list)    # [(price, label)]
    regime:        str   = "UNKNOWN"
    spot_ok:       bool  = False
    perp_ok:       bool  = False
    book_ok:       bool  = False
    last_poll_ts:  float = 0.0
    lock:          threading.Lock = field(default_factory=threading.Lock)


# ── Signal computation (identical to indicator_app._direction and helpers) ─────

def compute_ofi(buf: deque, window: float) -> float | None:
    cutoff = time.time() - window
    buy = sell = 0.0; n = 0
    for ts, qty, is_buy in buf:
        if ts < cutoff: continue
        n += 1
        if is_buy: buy += qty
        else:      sell += qty
    total = buy + sell
    return buy / total if n >= _MIN_TRADES and total > 1e-8 else None


def compute_liq(buf: deque) -> float | None:
    now    = time.time()
    recent = sum(q for ts, q, _ in buf if ts >= now - 30)
    pool   = [q for ts, q, _ in buf if now - _TRADE_WINDOW <= ts < now - 30]
    if not pool: return None
    baseline = sum(pool) / (min(270.0, _TRADE_WINDOW - 30) / 30)
    return recent / baseline if baseline > 1e-8 else None


def compute_cvd_slope(buf: deque, window: float = 60.0) -> float | None:
    now = time.time(); cutoff = now - window; mid = cutoff + window / 2
    early = sum((q if b else -q) for ts, q, b in buf if cutoff <= ts < mid)
    late  = sum((q if b else -q) for ts, q, b in buf if ts >= mid)
    total = sum(abs(q) for ts, q, _ in buf if ts >= cutoff)
    return (late - early) / total if total > 1e-8 else None


def compute_obi(c: Cache) -> float | None:
    with c.lock:
        if not c.bids or not c.asks: return None
        bid_vol = sum(c.bids[p] for p in sorted(c.bids, reverse=True)[:10])
        ask_vol = sum(c.asks[p] for p in sorted(c.asks)[:10])
    total = bid_vol + ask_vol
    return bid_vol / total if total > 0 else None


def compute_momentum(prices: deque, window: float = 45.0) -> float | None:
    now = time.time(); cutoff = now - window; mid = now - window / 2
    recent = [(p, q) for ts, p, q in prices if ts >= mid]
    early  = [(p, q) for ts, p, q in prices if cutoff <= ts < mid]
    if len(recent) < 3 or len(early) < 3: return None
    def vwap(lst: list) -> float:
        v = sum(q for _, q in lst)
        return sum(p * q for p, q in lst) / v if v > 1e-10 else 0.0
    r, e = vwap(recent), vwap(early)
    if e == 0: return None
    return max(-1.0, min(1.0, (r - e) / e * 100 / 0.05))


def compute_direction(c: Cache) -> tuple[str, float, dict]:
    """Directional signal. Returns (direction, score, signals).

    direction: "up" | "down" | "undecided"
    score: 0.0–1.0 (after regime multiplier)
    signals: {spot_ofi, perp_ofi, obi, cvd, mom, liq, regime}
    """
    with c.lock:
        spot_ofi = compute_ofi(c.spot, _OFI_WINDOW)
        perp_ofi = compute_ofi(c.perp, _OFI_WINDOW)
        liq_val  = compute_liq(c.spot)
        cvd      = compute_cvd_slope(c.spot, _OFI_WINDOW)
        regime   = c.regime
        prices   = c.prices

    mom = compute_momentum(prices, _MOM_WINDOW)
    obi = compute_obi(c)
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


# ── Support / Resistance (identical to indicator_app) ─────────────────────────

def compute_book_walls(c: Cache, threshold: float = 1.8) -> tuple[list, list]:
    """Large bid/ask clusters within 1.5% of current price."""
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


def compute_vpoc_levels(c: Cache, bucket: float = 100.0, n: int = 2) -> list[dict]:
    """Most-traded price buckets from the trade buffer."""
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
        if abs(p - price) / price < 0.005: continue
        out.append({"price": p, "vol": v, "label": "volume", "src": "vpoc"})
        if len(out) >= n: break
    return out


def compile_levels(c: Cache) -> tuple[float, list, list]:
    """All S/R sources merged and de-duplicated (nearest first)."""
    with c.lock:
        price = c.current_price
        ohlc  = list(c.ohlc_levels)

    if not price:
        return 0.0, [], []

    book_sup, book_res = compute_book_walls(c)
    vpoc = compute_vpoc_levels(c)

    supports    = list(book_sup)
    resistances = list(book_res)

    for p, label in ohlc:
        entry = {"price": p, "vol": 0, "label": label, "src": "ohlc"}
        if p < price:   supports.append(entry)
        elif p > price: resistances.append(entry)

    for lvl in vpoc:
        if lvl["price"] < price:   supports.append(lvl)
        elif lvl["price"] > price: resistances.append(lvl)

    supports    = sorted(supports,    key=lambda x: x["price"], reverse=True)
    resistances = sorted(resistances, key=lambda x: x["price"])

    def dedup(lst: list) -> list:
        out: list = []
        for lvl in lst:
            if not out or abs(lvl["price"] - out[-1]["price"]) > 200:
                out.append(lvl)
        return out

    return price, dedup(supports), dedup(resistances)


def wall_message(price: float, supports: list, resistances: list,
                 direction: str) -> tuple[str, str | None]:
    """(message, color_override) — identical to indicator_app._wall_message()."""
    if not price: return "", None

    sup = supports[0]    if supports    else None
    res = resistances[0] if resistances else None

    sup_pct = ((price - sup["price"]) / price * 100) if sup else 999.0
    res_pct = ((res["price"] - price) / price * 100) if res else 999.0

    if sup_pct < 0.12:
        if direction == "down":
            return f"Op support {sup['price']:,.0f} — kans op bounce!", "#f59e0b"
        return f"Support {sup['price']:,.0f} — bevestigt stijging", "#22c55e"

    if res_pct < 0.12:
        if direction == "up":
            return f"Op weerstand {res['price']:,.0f} — kan stagneren", "#f59e0b"
        return f"Weerstand {res['price']:,.0f} — bevestigt daling", "#ef4444"

    if direction == "down" and sup_pct < 0.35:
        return f"Nadert support {sup['price']:,.0f} (${price - sup['price']:,.0f})", "#f59e0b"

    if direction == "up" and res_pct < 0.35:
        return f"Nadert weerstand {res['price']:,.0f} (${res['price'] - price:,.0f})", "#f59e0b"

    if sup_pct < 0.35:
        return f"Support {sup['price']:,.0f} vlakbij (${price - sup['price']:,.0f})", None
    if res_pct < 0.35:
        return f"Weerstand {res['price']:,.0f} vlakbij (${res['price'] - price:,.0f})", None

    return "", None


# ── Background polling (identical to indicator_app._poll_loop) ─────────────────

async def _poll_loop(c: Cache) -> None:
    spot_pair = _COIN_TO_SPOT_PAIR.get(c.coin, "XBTUSD")
    perp_pair = _COIN_TO_PERP_PAIR.get(c.coin, "PF_XBTUSD")
    last_since   = ""
    last_regime  = 0.0
    fut_ts       = ""
    perp_counter = 0

    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            now = time.time()

            # Spot trades
            try:
                params: dict = {"pair": spot_pair}
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
                # Feed snap reversal detector outside the lock
                if c.current_price:
                    try:
                        from .snap_reversal import get_detector as _snap_det
                        _snap_det(c.coin).update(c.current_price)
                    except Exception:
                        pass
            except Exception:
                with c.lock: c.spot_ok = False

            # Order book (25 levels)
            try:
                r    = await client.get(_KRAKEN_REST + "/Depth",
                                        params={"pair": spot_pair, "count": 25})
                res  = r.json().get("result", {})
                book = next((v for k, v in res.items()), {})
                with c.lock:
                    c.bids    = {float(p): float(v) for p, v, *_ in book.get("bids", [])}
                    c.asks    = {float(p): float(v) for p, v, *_ in book.get("asks", [])}
                    c.book_ok = bool(c.bids and c.asks)
            except Exception:
                with c.lock: c.book_ok = False

            # Perp (every 2nd cycle)
            perp_counter += 1
            if perp_counter >= 2:
                perp_counter = 0
                try:
                    pf: dict = {"symbol": perp_pair}
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

            # Regime + OHLC pivots (every 2 min)
            if now - last_regime >= 120:
                last_regime = now
                try:
                    r = await client.get(_KRAKEN_REST + "/OHLC",
                                         params={"pair": spot_pair, "interval": 1})
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

                try:
                    r = await client.get(_KRAKEN_REST + "/OHLC",
                                         params={"pair": spot_pair, "interval": 1440})
                    res     = r.json().get("result", {})
                    candles = next((v for k, v in res.items() if k != "last"), [])
                    levels: list[tuple[float, str]] = []
                    if len(candles) >= 2:
                        prev = candles[-2]
                        levels += [(float(prev[2]), "gist.H"), (float(prev[3]), "gist.L")]
                        curr = candles[-1]
                        levels += [(float(curr[2]), "dag H"), (float(curr[3]), "dag L")]
                    with c.lock:
                        c.ohlc_levels = levels
                except Exception:
                    pass

            await asyncio.sleep(_POLL_SECS)


def _run_loop(c: Cache) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_poll_loop(c))


# ── Singleton registry ─────────────────────────────────────────────────────────

_caches: dict[str, Cache] = {}
_lock = threading.Lock()


def get_cache(coin: str = "BTC") -> Cache:
    """Return per-coin cache singleton. Starts polling thread on first call."""
    with _lock:
        if coin not in _caches:
            c = Cache(coin=coin)
            _caches[coin] = c
            threading.Thread(target=_run_loop, args=(c,), daemon=True).start()
        return _caches[coin]
