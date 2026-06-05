#!/usr/bin/env python3
"""
Stoplicht Backtest — laatste 48 uur, 4 strategieën vergeleken.

Modus 1 (standaard): haalt echte Polymarket + Kraken data op.
  Gebruik op de server: python stoplicht_backtest_48h.py

Modus 2 (--synthetic): genereert realistische synthetische data.
  Gebruik lokaal of zonder netwerk: python stoplicht_backtest_48h.py --synthetic

Strategieën:
  1. Stoplicht Scalper  (BTC-updown-15m, €5)  — momentum-gebaseerd directioneel
  2. BGGDSB             (BTC-updown-5m,  €2)  — dominante kant + resolutie
  3. Signal Trader      (BTC-updown-5m,  €10) — conviction directioneel
  4. Auto Router        (BTC-updown-5m)        — combinatie BGGDSB + Signal

Stoplicht signal proxy: Kraken BTC 1-min OHLC (OFI/OBI/MOM/CVD proxy).
  Score ≥ 0.60 = GROEN, score ≥ 0.35 = ORANJE, anders ROOD.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── Parameters ────────────────────────────────────────────────────────────────
SCALPER_SIZE_EUR    = 5.0
SCALPER_ENTRY_ADD   = 0.01
SCALPER_TRAIL_ACT   = 0.07    # +7¢ activeer trailing
SCALPER_TRAIL_BUF   = 0.02    # -2¢ van piek = stop

BGGDSB_BUDGET_EUR   = 2.0
BGGDSB_DOM_RATIO    = 0.875
BGGDSB_ENTRY_MIN    = 0.40
BGGDSB_ENTRY_MAX    = 0.65

SIGNAL_SIZE_EUR     = 10.0
SIGNAL_CONVICTION   = 0.50
SIGNAL_ENTRY_MAX    = 0.65

ROUTER_SIGNAL_MIN   = 0.55

FEE_RATE            = 0.018

GAMMA_URL  = "https://gamma-api.polymarket.com"
CLOB_URL   = "https://clob.polymarket.com"
KRAKEN_URL = "https://api.kraken.com/0/public"


# ══════════════════════════════════════════════════════════════════════════════
#  MODUS 2: SYNTHETISCHE DATA (realistisch Monte Carlo)
# ══════════════════════════════════════════════════════════════════════════════

def generate_btc_price_path(n_minutes: int, start_price: float = 104_000,
                             vol_per_min: float = 0.0015,
                             seed: int = 42) -> list[float]:
    """Geometrische Brownian motion met typische BTC volatiliteit."""
    rng = random.Random(seed)
    prices = [start_price]
    for _ in range(n_minutes):
        ret  = rng.gauss(0, vol_per_min)
        new  = prices[-1] * math.exp(ret)
        prices.append(new)
    return prices


def btc_to_polymarket_mid(btc_return_pct: float, noise: float = 0.05,
                           rng: random.Random = None) -> float:
    """Converteer BTC rendement naar Polymarket YES mid prijs.

    Bij 0% rendement → ~0.50. Bij +0.3% → ~0.65. Bij -0.3% → ~0.35.
    Met realistische markt-ruis.
    """
    if rng is None:
        rng = random.Random()
    sensitivity = 50.0  # hoe snel market meebeweegt met BTC
    raw = 0.50 + btc_return_pct * sensitivity
    raw += rng.gauss(0, noise)
    return max(0.05, min(0.95, raw))


def compute_stoplicht_from_btc(btc_prices: list[float], minute: int,
                                lookback: int = 5) -> tuple[str, str, float]:
    """Bereken stoplicht signaal proxy van BTC-koersbeweging.

    Simuleert dezelfde formule als de echte indicator maar op basis van
    prijs in plaats van echte order flow data.
    """
    start_idx = max(0, minute - lookback)
    if minute < lookback or start_idx >= len(btc_prices):
        return "ROOD", None, 0.0

    chunk = btc_prices[start_idx:minute + 1]
    if len(chunk) < 2:
        return "ROOD", None, 0.0

    # MOM: totale prijsverandering
    mom_pct = (chunk[-1] - chunk[0]) / chunk[0] * 100

    # OFI proxy: hoeveel stijgende perioden
    bull_bars = sum(1 for i in range(1, len(chunk)) if chunk[i] >= chunk[i-1])
    ofi_proxy = bull_bars / (len(chunk) - 1)

    # OBI proxy: laatste prijs vs range
    hi, lo = max(chunk), min(chunk)
    obi_proxy = (chunk[-1] - lo) / (hi - lo) if hi > lo else 0.5

    # CVD proxy
    cvd = sum(chunk[i] - chunk[i-1] for i in range(1, len(chunk)))
    cvd = max(-1.0, min(1.0, cvd / chunk[0] * 300))

    # Score (zelfde formule als indicator_engine.py compute_direction)
    bull = bear = 0.0

    if ofi_proxy > 0.60:
        bull += (ofi_proxy - 0.60) / 0.40 * 0.35
    elif ofi_proxy < 0.40:
        bear += (0.40 - ofi_proxy) / 0.40 * 0.35

    if obi_proxy > 0.65:
        bull += (obi_proxy - 0.65) / 0.35 * 0.28
    elif obi_proxy < 0.35:
        bear += (0.35 - obi_proxy) / 0.35 * 0.28

    if abs(mom_pct) > 0.03:
        w = min(0.20, abs(mom_pct) / 0.20 * 0.20)
        if mom_pct > 0: bull += w
        else:           bear += w

    if abs(cvd) > 0.10:
        w = min(0.05, abs(cvd) * 0.08)
        if cvd > 0: bull += w
        else:       bear += w

    # Regime multiplier
    range_pct = (hi - lo) / chunk[-1] * 100 if chunk[-1] > 0 else 1.0
    mult = (1.15 if range_pct < 0.20 else
            0.85 if range_pct > 0.80 else 1.00)
    bull *= mult
    bear *= mult

    score = round(min(1.0, max(bull, bear)), 3)
    if score < 0.15:
        return "ROOD", None, 0.0

    direction = "UP" if bull >= bear else "DOWN"
    color = ("GROEN"  if score >= 0.60 else
             "ORANJE" if score >= 0.35 else "ROOD")
    return color, direction, score


def generate_synthetic_markets(btc_prices: list[float],
                                window_minutes: int, n_windows: int,
                                offset_minutes: int = 0,
                                seed_base: int = 100) -> list[dict]:
    """Genereer synthetische marktdata op basis van BTC prijspad."""
    now = datetime.now(timezone.utc)
    base_ts = now - timedelta(hours=48)
    markets = []
    rng = random.Random(seed_base)

    for i in range(n_windows):
        win_start_min = offset_minutes + i * window_minutes
        win_end_min   = win_start_min + window_minutes

        if win_end_min >= len(btc_prices):
            break

        ws = base_ts + timedelta(minutes=win_start_min)
        we = base_ts + timedelta(minutes=win_end_min)

        # BTC rendement over het window → bepaal uitkomst
        btc_start = btc_prices[win_start_min]
        btc_end   = btc_prices[win_end_min]
        btc_ret   = (btc_end - btc_start) / btc_start

        # Outcome: stochastisch maar gecorreleerd met BTC richting
        # Grotere beweging → hogere kans dat het overeenkomt
        base_prob_up = 0.50 + btc_ret * 25  # 0.3% BTC move → 57.5% kans YES
        base_prob_up = max(0.05, min(0.95, base_prob_up))
        outcome = "YES" if rng.random() < base_prob_up else "NO"

        # Genereer prijspad voor YES token (gecorreleerd met BTC)
        yes_prices = []
        no_prices  = []
        for t in range(win_end_min - win_start_min + 2):
            min_idx = win_start_min - 1 + t
            if 0 <= min_idx < len(btc_prices):
                btc_r = (btc_prices[min_idx] - btc_start) / btc_start
                yes_mid = btc_to_polymarket_mid(btc_r, noise=0.03, rng=rng)
                no_mid  = 1 - yes_mid
                spread  = rng.uniform(0.02, 0.06)
                yes_bid = max(0.01, yes_mid - spread / 2)
                no_bid  = max(0.01, no_mid  - spread / 2)
                ts = int((ws + timedelta(minutes=t)).timestamp())
                yes_prices.append({"t": ts, "p": yes_bid})
                no_prices.append( {"t": ts, "p": no_bid})

        # Uiteindelijke prijs: winner naar 1.0, loser naar 0.0
        final_ts = int(we.timestamp()) + 60
        yes_prices.append({"t": final_ts, "p": 1.0 if outcome == "YES" else 0.0})
        no_prices.append( {"t": final_ts, "p": 0.0 if outcome == "YES" else 1.0})

        # Stoplicht signaal
        color, direction, score = compute_stoplicht_from_btc(
            btc_prices, win_start_min)

        markets.append({
            "slug":         f"btc-updown-{window_minutes}m-{int(ws.timestamp())}",
            "window_start": ws,
            "window_end":   we,
            "yes_prices":   yes_prices,
            "no_prices":    no_prices,
            "outcome":      outcome,
            "stoplicht_color": color,
            "stoplicht_dir":   direction,
            "stoplicht_score": score,
            "btc_ret_pct":  round(btc_ret * 100, 3),
        })

    return markets


# ══════════════════════════════════════════════════════════════════════════════
#  MODUS 1: ECHTE DATA (Polymarket + Kraken)
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_closed_markets(client, slug_filter: str, hours: int = 48) -> list[dict]:
    now   = datetime.now(timezone.utc)
    start = (now - timedelta(hours=hours)).isoformat()
    params = {
        "closed": "true", "limit": 500,
        "order": "endDate", "ascending": "true",
        "end_date_min": start, "end_date_max": now.isoformat(),
    }
    try:
        r      = await client.get(f"{GAMMA_URL}/events", params=params, timeout=20)
        events = r.json()
        if not isinstance(events, list):
            events = events.get("events", [])
    except Exception as e:
        print(f"[WARN] Gamma fetch fout: {e}")
        return []

    results = []
    for ev in events:
        slug = (ev.get("slug") or "").lower()
        if slug_filter.lower() not in slug:
            continue
        parts = slug.rsplit("-", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        try:
            window_start = datetime.fromtimestamp(int(parts[1]), tz=timezone.utc)
            m   = re.search(r"-(\d+)m$", parts[0])
            dur = int(m.group(1)) if m else 5
            window_end = window_start + timedelta(minutes=dur)
        except Exception:
            continue

        for mkt in ev.get("markets") or []:
            clob_ids = mkt.get("clobTokenIds")
            if isinstance(clob_ids, str):
                try:
                    clob_ids = json.loads(clob_ids)
                except Exception:
                    clob_ids = None
            if not clob_ids or len(clob_ids) < 2:
                continue

            outcome_raw = mkt.get("outcomePrices") or ""
            if isinstance(outcome_raw, str):
                try:
                    prices  = json.loads(outcome_raw)
                    outcome = "YES" if float(prices[0]) > 0.5 else "NO"
                except Exception:
                    outcome = None
            elif isinstance(outcome_raw, list):
                try:
                    outcome = "YES" if float(outcome_raw[0]) > 0.5 else "NO"
                except Exception:
                    outcome = None
            else:
                outcome = None

            if outcome is None:
                continue

            results.append({
                "slug": slug, "window_start": window_start, "window_end": window_end,
                "yes_token": clob_ids[0], "no_token": clob_ids[1],
                "condition_id": mkt.get("conditionId"), "outcome": outcome,
            })
    return results


async def fetch_token_prices(client, token_id: str,
                              start_ts: int, end_ts: int) -> list[dict]:
    try:
        r = await client.get(
            f"{CLOB_URL}/prices-history",
            params={"market": token_id, "startTs": start_ts,
                    "endTs": end_ts, "fidelity": 1},
            timeout=15,
        )
        data = r.json()
        history = data.get("history", []) if isinstance(data, dict) else (data or [])
        return [{"t": int(h.get("t") or 0), "p": float(h.get("p") or 0)}
                for h in history if h.get("t") and h.get("p")]
    except Exception:
        return []


async def fetch_kraken_ohlc(client) -> list[dict]:
    since = int((datetime.now(timezone.utc) - timedelta(hours=50)).timestamp())
    try:
        r = await client.get(
            f"{KRAKEN_URL}/OHLC",
            params={"pair": "XBTUSD", "interval": 1, "since": since},
            timeout=15,
        )
        res = r.json().get("result", {})
        raw = next((v for k, v in res.items() if k != "last"), [])
        return [{"ts": int(c[0]), "close": float(c[4]), "vol": float(c[6]),
                 "open": float(c[1]), "high": float(c[2]), "low": float(c[3])}
                for c in raw]
    except Exception as e:
        print(f"[WARN] Kraken OHLC fout: {e}")
        return []


def compute_stoplicht_proxy_ohlc(candle_idx: dict, window_start: datetime,
                                   lookback: int = 5) -> tuple[str, str, float]:
    ts = int(window_start.timestamp())
    candles = []
    for i in range(lookback, 0, -1):
        c = candle_idx.get((ts // 60 - i) * 60)
        if c:
            candles.append(c)
    if len(candles) < 2:
        return "ROOD", None, 0.0

    start_p = candles[0]["open"]
    end_p   = candles[-1]["close"]
    if start_p <= 0:
        return "ROOD", None, 0.0
    mom_pct   = (end_p - start_p) / start_p * 100
    bull_bars = sum(1 for c in candles if c["close"] >= c["open"])
    ofi_proxy = bull_bars / len(candles)
    hi = max(c["high"] for c in candles)
    lo = min(c["low"]  for c in candles)
    rng_c = hi - lo
    obi_proxy = (end_p - lo) / rng_c if rng_c > 0.01 else 0.5
    cvd = sum(c["close"] - c["open"] for c in candles)
    cvd = max(-1.0, min(1.0, cvd / start_p * 300))

    bull = bear = 0.0
    if ofi_proxy > 0.60: bull += (ofi_proxy - 0.60) / 0.40 * 0.35
    elif ofi_proxy < 0.40: bear += (0.40 - ofi_proxy) / 0.40 * 0.35
    if obi_proxy > 0.65: bull += (obi_proxy - 0.65) / 0.35 * 0.28
    elif obi_proxy < 0.35: bear += (0.35 - obi_proxy) / 0.35 * 0.28
    if abs(mom_pct) > 0.03:
        w = min(0.20, abs(mom_pct) / 0.20 * 0.20)
        if mom_pct > 0: bull += w
        else: bear += w
    if abs(cvd) > 0.10:
        w = min(0.05, abs(cvd) * 0.08)
        if cvd > 0: bull += w
        else: bear += w

    range_pct = (hi - lo) / end_p * 100 if end_p > 0 else 1.0
    mult = 1.15 if range_pct < 0.20 else (0.85 if range_pct > 0.80 else 1.00)
    bull *= mult; bear *= mult
    score = round(min(1.0, max(bull, bear)), 3)
    if score < 0.15:
        return "ROOD", None, 0.0
    direction = "UP" if bull >= bear else "DOWN"
    color = "GROEN" if score >= 0.60 else "ORANJE" if score >= 0.35 else "ROOD"
    return color, direction, score


# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGIE SIMULATIES (gemeenschappelijk voor beide modi)
# ══════════════════════════════════════════════════════════════════════════════

def price_at(history: list[dict], target_ts: int, default: float = 0.50) -> float:
    if not history:
        return default
    best = min(history, key=lambda h: abs(h["t"] - target_ts))
    return best["p"]


def simulate_scalper(market: dict, color: str, direction: str, score: float) -> dict | None:
    if color != "GROEN" or not direction:
        return None
    ws = int(market["window_start"].timestamp())
    we = int(market["window_end"].timestamp())

    yes_prices = market["yes_prices"]
    no_prices  = market["no_prices"]

    entry_ts = ws + 15
    if direction == "UP":
        entry_p = price_at(yes_prices, entry_ts, 0.52) + SCALPER_ENTRY_ADD
        token   = "YES"
        chosen  = yes_prices
    else:
        entry_p = price_at(no_prices, entry_ts, 0.52) + SCALPER_ENTRY_ADD
        token   = "NO"
        chosen  = no_prices

    if not (0.20 <= entry_p <= 0.80):
        return None

    shares    = SCALPER_SIZE_EUR / entry_p
    fee_entry = FEE_RATE * entry_p * shares

    peak = entry_p; trailing = False
    exit_p = None; exit_r = "resolution"

    for snap in sorted(chosen, key=lambda h: h["t"]):
        if snap["t"] <= entry_ts or snap["t"] > we:
            continue
        p = snap["p"]
        if p > peak:
            peak = p
        if not trailing and peak >= entry_p + SCALPER_TRAIL_ACT:
            trailing = True
        if trailing and p <= peak - SCALPER_TRAIL_BUF:
            exit_p = p; exit_r = "trail_stop"; break

    if exit_p is None:
        exit_p = 1.0 if token == market["outcome"] else 0.0

    gross    = (exit_p - entry_p) * shares
    fee_exit = (FEE_RATE * min(exit_p, 1-exit_p) / 0.5 * shares * exit_p
                if exit_p < 1.0 else 0.0)
    net_pnl  = gross - fee_entry - fee_exit

    return {
        "strategy": "Scalper", "window": market["window_start"].strftime("%d/%m %H:%M"),
        "direction": direction, "outcome": market["outcome"],
        "correct": (direction == "UP") == (market["outcome"] == "YES"),
        "entry_p": round(entry_p, 3), "exit_p": round(exit_p, 3),
        "exit_r": exit_r, "score": score,
        "net_pnl": round(net_pnl, 4), "won": net_pnl > 0,
    }


def simulate_bggdsb(market: dict, direction: str | None) -> dict | None:
    ws = int(market["window_start"].timestamp())
    entry_ts = ws + 2
    yes_p = price_at(market["yes_prices"], entry_ts, 0.52) + 0.01
    no_p  = price_at(market["no_prices"],  entry_ts, 0.52) + 0.01

    yes_ok = BGGDSB_ENTRY_MIN <= yes_p <= BGGDSB_ENTRY_MAX
    no_ok  = BGGDSB_ENTRY_MIN <= no_p  <= BGGDSB_ENTRY_MAX
    if not yes_ok and not no_ok:
        return None

    if direction == "UP":   dom = "YES"
    elif direction == "DOWN": dom = "NO"
    else: dom = "YES" if yes_p <= no_p else "NO"

    dom_p   = yes_p if dom == "YES" else no_p
    hedge_p = no_p  if dom == "YES" else yes_p
    dom_eur, hedge_eur = BGGDSB_BUDGET_EUR * BGGDSB_DOM_RATIO, BGGDSB_BUDGET_EUR * (1 - BGGDSB_DOM_RATIO)
    dom_sh   = dom_eur   / dom_p   if dom_p   > 0 else 0
    hedge_sh = hedge_eur / hedge_p if hedge_p > 0 else 0

    yes_sh = dom_sh if dom == "YES" else hedge_sh
    no_sh  = hedge_sh if dom == "YES" else dom_sh

    fee     = (yes_p * yes_sh + no_p * no_sh) * FEE_RATE
    payout  = yes_sh if market["outcome"] == "YES" else no_sh
    gross   = payout - BGGDSB_BUDGET_EUR
    net_pnl = gross - fee

    return {
        "strategy": "BGGDSB", "window": market["window_start"].strftime("%d/%m %H:%M"),
        "direction": direction, "dominant": dom, "outcome": market["outcome"],
        "correct": dom == market["outcome"],
        "entry_yes": round(yes_p, 3), "entry_no": round(no_p, 3),
        "net_pnl": round(net_pnl, 4), "won": net_pnl > 0,
    }


def simulate_signal_trader(market: dict, direction: str | None, score: float) -> dict | None:
    if not direction or score < SIGNAL_CONVICTION:
        return None
    ws = int(market["window_start"].timestamp())
    entry_ts = ws + 5

    if direction == "UP":
        entry_p = price_at(market["yes_prices"], entry_ts, 0.52) + 0.01
        token   = "YES"
    else:
        entry_p = price_at(market["no_prices"], entry_ts, 0.52) + 0.01
        token   = "NO"

    if entry_p > SIGNAL_ENTRY_MAX or entry_p < 0.10:
        return None

    shares  = SIGNAL_SIZE_EUR / entry_p
    fee     = FEE_RATE * entry_p * shares
    payout  = shares if token == market["outcome"] else 0.0
    gross   = payout - SIGNAL_SIZE_EUR
    net_pnl = gross - fee

    return {
        "strategy": "Signal", "window": market["window_start"].strftime("%d/%m %H:%M"),
        "direction": direction, "token": token, "outcome": market["outcome"],
        "correct": token == market["outcome"],
        "entry_p": round(entry_p, 3), "score": score,
        "net_pnl": round(net_pnl, 4), "won": net_pnl > 0,
    }


def simulate_router(market: dict, direction: str | None, score: float) -> dict | None:
    if direction and score >= ROUTER_SIGNAL_MIN:
        t = simulate_signal_trader(market, direction, score)
        if t:
            t["strategy"] = "Router→Signal"
            return t
    t = simulate_bggdsb(market, direction)
    if t:
        t["strategy"] = "Router→BGGDSB"
    return t


# ══════════════════════════════════════════════════════════════════════════════
#  OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

def print_comparison_table(all_results: dict[str, list[dict]]) -> None:
    print("\n" + "═" * 82)
    print("  SAMENVATTING — ALLE STRATEGIEËN")
    print("═" * 82)
    print(f"  {'Strategie':<26} {'Trades':>7} {'WR%':>6} {'Correct%':>9} "
          f"{'P&L':>10} {'P&L/trade':>10} {'MaxDD':>7}")
    print("  " + "─" * 78)
    for name, trades in all_results.items():
        n = len(trades)
        if n == 0:
            print(f"  {name:<26} {'—':>7}")
            continue
        wins    = sum(1 for t in trades if t.get("won"))
        correct = sum(1 for t in trades if t.get("correct"))
        total   = sum(t["net_pnl"] for t in trades)
        avg     = total / n
        best = dd = running = 0.0
        for t in trades:
            running += t["net_pnl"]
            if running > best: best = running
            d = best - running
            if d > dd: dd = d
        sign = "+" if total >= 0 else ""
        print(f"  {name:<26} {n:>7} {wins/n*100:>5.1f}% {correct/n*100:>8.1f}% "
              f"{sign}{total:>9.2f} {avg:>+10.4f} {dd:>7.2f}")
    print("═" * 82)


def print_detail(name: str, trades: list[dict]) -> None:
    n = len(trades)
    if n == 0:
        return
    wins    = sum(1 for t in trades if t.get("won"))
    correct = sum(1 for t in trades if t.get("correct"))
    total   = sum(t["net_pnl"] for t in trades)

    best = dd = running = 0.0
    for t in trades:
        running += t["net_pnl"]
        if running > best: best = running
        d = best - running
        if d > dd: dd = d

    print(f"\n  ┌─ {name} {'─' * max(0, 42-len(name))}┐")
    print(f"  │ Trades       : {n}")
    print(f"  │ Win rate     : {wins}/{n} = {wins/n*100:.1f}%")
    print(f"  │ Richting ok  : {correct}/{n} = {correct/n*100:.1f}%")
    print(f"  │ Totaal P&L   : €{total:+.2f}")
    print(f"  │ Gem. P&L     : €{total/n:+.4f}")
    print(f"  │ Max drawdown : €{dd:.2f}")
    print(f"  └{'─' * 46}┘")

    # Per richting
    for dir_label in ("UP", "DOWN"):
        dt = [t for t in trades if t.get("direction") == dir_label]
        if dt:
            dp = sum(t["net_pnl"] for t in dt)
            dw = sum(1 for t in dt if t["won"])
            print(f"    {dir_label:<5} {len(dt):>3} trades  WR={dw/len(dt)*100:>5.1f}%  P&L=€{dp:+.2f}")

    # Exit breakdown (scalper)
    exits = defaultdict(int)
    for t in trades:
        if "exit_r" in t:
            exits[t["exit_r"]] += 1
    if exits:
        print(f"    Exits: " + "  ".join(f"{k}={v}" for k, v in exits.items()))


def print_signal_analysis(markets_15m: list[dict], markets_5m: list[dict],
                           pre_computed: bool = False) -> None:
    print("\n" + "═" * 60)
    print("  STOPLICHT SIGNAL ANALYSE")
    print("═" * 60)

    for label, markets in [("15-min markten", markets_15m), ("5-min markten", markets_5m)]:
        if not markets:
            continue
        signals = []
        for mkt in markets:
            color = mkt.get("stoplicht_color")
            direction = mkt.get("stoplicht_dir")
            score = mkt.get("stoplicht_score", 0)
            if color in ("GROEN", "ORANJE") and direction:
                correct = (direction == "UP") == (mkt["outcome"] == "YES")
                signals.append({"color": color, "direction": direction,
                                 "score": score, "correct": correct})

        if not signals:
            continue

        n  = len(signals)
        ok = sum(1 for s in signals if s["correct"])
        groen = [s for s in signals if s["color"] == "GROEN"]
        ok_g  = sum(1 for s in groen if s["correct"])
        oranje = [s for s in signals if s["color"] == "ORANJE"]
        ok_o  = sum(1 for s in oranje if s["correct"])
        rood_n = len(markets) - n

        print(f"\n  {label} ({len(markets)} windows):")
        print(f"    ROOD  (geen signaal): {rood_n:>4} windows  ({rood_n/len(markets)*100:.1f}%)")
        if oranje:
            print(f"    ORANJE              : {len(oranje):>4} windows  "
                  f"accuracy={ok_o/len(oranje)*100:.1f}%")
        if groen:
            print(f"    GROEN               : {len(groen):>4} windows  "
                  f"accuracy={ok_g/len(groen)*100:.1f}%")
        print(f"    Alle signalen       : {n:>4} windows  accuracy={ok/n*100:.1f}%")
        avg_score = sum(s["score"] for s in signals) / n
        print(f"    Gem. score          : {avg_score:.3f}")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def run_real_data() -> None:
    """Modus 1: echte Polymarket + Kraken data."""
    try:
        import httpx
    except ImportError:
        print("[FOUT] httpx niet geïnstalleerd: pip install httpx")
        return

    print("\n[1/4] Markten ophalen van Gamma API...")
    async with httpx.AsyncClient(timeout=30) as client:
        markets_15m = await fetch_closed_markets(client, "btc-updown-15m")
        markets_5m  = await fetch_closed_markets(client, "btc-updown-5m")
        print(f"      BTC-15m: {len(markets_15m)}  BTC-5m: {len(markets_5m)}")

        if not markets_15m and not markets_5m:
            print("[FOUT] Geen marktdata. Gebruik --synthetic voor lokale test.")
            return

        print("\n[2/4] Kraken OHLC ophalen...")
        candles = await fetch_kraken_ohlc(client)
        candle_idx = {c["ts"]: c for c in candles}
        print(f"      {len(candles)} candles geladen")

        print("\n[3/4] Prijsgeschiedenissen ophalen...")
        cache: dict = {}

        async def get_prices(token: str, ws: int, we: int) -> list[dict]:
            k = f"{token}_{ws}"
            if k not in cache:
                cache[k] = await fetch_token_prices(client, token, ws-60, we+60)
            return cache[k]

        for i, mkt in enumerate(markets_15m):
            sys.stdout.write(f"\r      15m: {i+1}/{len(markets_15m)}")
            sys.stdout.flush()
            ws = int(mkt["window_start"].timestamp())
            we = int(mkt["window_end"].timestamp())
            mkt["yes_prices"] = await get_prices(mkt["yes_token"], ws, we)
            mkt["no_prices"]  = await get_prices(mkt["no_token"],  ws, we)
            c, d, s = compute_stoplicht_proxy_ohlc(candle_idx, mkt["window_start"])
            mkt["stoplicht_color"] = c
            mkt["stoplicht_dir"]   = d
            mkt["stoplicht_score"] = s
        if markets_15m:
            print()

        for i, mkt in enumerate(markets_5m):
            sys.stdout.write(f"\r      5m:  {i+1}/{len(markets_5m)}")
            sys.stdout.flush()
            ws = int(mkt["window_start"].timestamp())
            we = int(mkt["window_end"].timestamp())
            mkt["yes_prices"] = await get_prices(mkt["yes_token"], ws, we)
            mkt["no_prices"]  = await get_prices(mkt["no_token"],  ws, we)
            c, d, s = compute_stoplicht_proxy_ohlc(candle_idx, mkt["window_start"])
            mkt["stoplicht_color"] = c
            mkt["stoplicht_dir"]   = d
            mkt["stoplicht_score"] = s
        if markets_5m:
            print()

    _simulate_and_print(markets_15m, markets_5m)


def run_synthetic() -> None:
    """Modus 2: synthetische data op basis van Monte Carlo BTC prijspad."""
    print("\n  [SYNTHETISCHE MODUS] — Monte Carlo BTC prijspad (48h, vol 0.15%/min)")
    print("  Resultaten representeren een statistische verwachting,")
    print("  niet de exacte performance van gisteren/eergisteren.\n")

    # Genereer BTC prijspad: 48h × 60min = 2880 minuten
    btc_prices = generate_btc_price_path(
        n_minutes=2900, start_price=104_000,
        vol_per_min=0.0015, seed=42
    )

    # 15-min windows: 2880 / 15 = 192 windows
    markets_15m = generate_synthetic_markets(btc_prices, 15, 192, seed_base=200)
    # 5-min windows: 2880 / 5 = 576 windows
    markets_5m  = generate_synthetic_markets(btc_prices, 5, 576, seed_base=300)

    print(f"  BTC start: ${btc_prices[0]:,.0f} → eind: ${btc_prices[-1]:,.0f}  "
          f"(rendement: {(btc_prices[-1]/btc_prices[0]-1)*100:+.1f}%)")
    print(f"  15-min windows: {len(markets_15m)}  |  5-min windows: {len(markets_5m)}")

    _simulate_and_print(markets_15m, markets_5m)


def _simulate_and_print(markets_15m: list[dict], markets_5m: list[dict]) -> None:
    print("\n[Simulatie] Strategieën draaien...")

    scalper_trades = []
    bggdsb_trades  = []
    signal_trades  = []
    router_trades  = []

    for mkt in markets_15m:
        c = mkt["stoplicht_color"]
        d = mkt["stoplicht_dir"]
        s = mkt["stoplicht_score"]
        t = simulate_scalper(mkt, c, d, s)
        if t:
            scalper_trades.append(t)

    for mkt in markets_5m:
        c = mkt["stoplicht_color"]
        d = mkt["stoplicht_dir"]
        s = mkt["stoplicht_score"]
        t = simulate_bggdsb(mkt, d)
        if t: bggdsb_trades.append(t)
        t = simulate_signal_trader(mkt, d, s)
        if t: signal_trades.append(t)
        t = simulate_router(mkt, d, s)
        if t: router_trades.append(t)

    print(f"  Scalper: {len(scalper_trades)}  BGGDSB: {len(bggdsb_trades)}  "
          f"Signal: {len(signal_trades)}  Router: {len(router_trades)}")

    # Print resultaten
    print_comparison_table({
        "Scalper  (15m, €5/trade)":  scalper_trades,
        "BGGDSB   (5m, €2/trade)":   bggdsb_trades,
        "Signal   (5m, €10/trade)":  signal_trades,
        "AutoRouter(5m)":             router_trades,
    })

    print_detail("Stoplicht Scalper (15m, €5)", scalper_trades)
    print_detail("BGGDSB (5m, €2)",             bggdsb_trades)
    print_detail("Signal Trader (5m, €10)",      signal_trades)
    print_detail("Auto Router (5m)",              router_trades)

    print_signal_analysis(markets_15m, markets_5m)

    # ── Combinatie: Scalper + BGGDSB tegelijk ─────────────────────────────────
    all_combo = scalper_trades + bggdsb_trades
    if all_combo:
        total_combo = sum(t["net_pnl"] for t in all_combo)
        combo_wins  = sum(1 for t in all_combo if t["won"])
        print(f"\n  ──────────────────────────────────────────")
        print(f"  COMBO (Scalper 15m + BGGDSB 5m tegelijk):")
        print(f"    Totaal trades : {len(all_combo)}")
        print(f"    Win rate      : {combo_wins/len(all_combo)*100:.1f}%")
        print(f"    Totaal P&L    : €{total_combo:+.2f}")
        print(f"    Gem. P&L      : €{total_combo/len(all_combo):+.4f}")

    print("\n" + "═" * 60)
    print("  NOOT: Stoplicht proxy = Kraken OHLC momentum only.")
    print("  Echte indicator voegt OFI/OBI/perp-flow/CVD toe.")
    print("  Echte GROEN accuracy is typisch hoger dan deze proxy.")
    print("═" * 60)


if __name__ == "__main__":
    synthetic = "--synthetic" in sys.argv
    print("=" * 60)
    print("  STOPLICHT BACKTEST — laatste 48 uur")
    print(f"  Datum: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Modus: {'SYNTHETISCH (Monte Carlo)' if synthetic else 'LIVE DATA (Polymarket + Kraken)'}")
    print("=" * 60)

    if synthetic:
        run_synthetic()
    else:
        asyncio.run(run_real_data())
