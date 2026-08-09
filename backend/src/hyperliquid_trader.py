"""Hyperliquid Stoplicht Trader — perpetuals, geen tijdsdruk.

Dezelfde signaallogica als de stoplicht_scalper, maar aangepast voor perps:
  - Geen 15-minuten windows — positie loopt totdat exit-signaal vuur
  - LONG bij "up"-signaal, SHORT bij "down"-signaal
  - Trail stop, MOM-reversal en profit target in % (niet in ¢)
  - Geen Phase 2 / window-einde logica
  - Confirmed: OFI > 0.55 EN OBI > 0.60 (in dezelfde richting)
  - MOM-reversal hold: als trailing nooit actief was EN positie op verlies staat
    → houden (zelfde logica als stoplicht_scalper fix)
  - Positieherstel bij herstart: haalt open Hyperliquid-positie op
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field

from .config_loader import CONFIG
from .indicator_engine import get_cache, compute_direction, compile_levels
from .logger import log


# ── Positietracking ────────────────────────────────────────────────────────────

@dataclass
class HLPosition:
    direction: str        # "LONG" of "SHORT"
    entry_price: float
    size_usdc: float      # margin op moment van entry
    size_coin: float      # BTC/ETH/etc
    peak_price: float     # beste prijs gezien (max voor LONG, min voor SHORT)
    trailing_active: bool = False
    opened_at: float = field(default_factory=time.time)
    trades_count: int = 0  # aantal trades dit processo-leven (voor re-entry logica)

    def pnl_pct(self, current: float) -> float:
        if self.direction == "LONG":
            return (current - self.entry_price) / self.entry_price * 100
        return (self.entry_price - current) / self.entry_price * 100


_positions: dict[str, HLPosition | None] = {}

# Cache: voorkomt dat Claude meerdere keren wordt aangeroepen voor hetzelfde signaal.
# Sleutel: coin|direction|prijsniveau (per $500 bucket) — vervalt na 5 minuten.
_claude_cache: dict[str, tuple[str, float]] = {}  # key → (verdict, timestamp)


# ── Config helper ──────────────────────────────────────────────────────────────

def _cfg() -> dict:
    return CONFIG.get("hyperliquid_trader", {})


# ── Claude pre-entry filter ────────────────────────────────────────────────────

async def _claude_filter(
    coin: str, direction: str, score: float, price: float,
    signals: dict, supports: list, resistances: list, regime: str,
) -> str:
    """Roept Claude Haiku 4.5 aan vóór elke entry. Retourneert 'go', 'hold' of 'block'.

    Cache per coin+richting+prijsniveau (per $500), geldig 5 minuten.
    Bij elke fout of timeout: retourneert 'go' (blokkeert nooit ten onrechte).
    """
    import os
    import json as _json
    import re as _re
    import httpx as _httpx

    if not _cfg().get("claude_entry_filter", True):
        return "go"

    api_key = os.getenv("ANTHROPIC_API_KEY") or CONFIG.get("claude", {}).get("api_key", "")
    if not api_key or api_key.startswith("${"):
        return "go"

    # Cache-sleutel: coin + richting + prijsbucket van $500
    price_bucket = int(price / 500) * 500
    cache_key = f"{coin}|{direction}|{price_bucket}"
    cached = _claude_cache.get(cache_key)
    if cached and (time.time() - cached[1]) < 300:  # 5 minuten geldig
        log.info("hl_claude_filter_cached", coin=coin, direction=direction,
                 verdict=cached[0])
        return cached[0]

    def _fmt(v) -> str:
        return f"{v:.3f}" if v is not None else "n/b"

    sup_str = f"${supports[0]['price']:,.0f}"   if supports    else "—"
    res_str = f"${resistances[0]['price']:,.0f}" if resistances else "—"
    ofi     = _fmt(signals.get("spot_ofi"))
    obi     = _fmt(signals.get("obi"))
    mom     = _fmt(signals.get("mom"))
    cvd     = _fmt(signals.get("cvd"))

    system = (
        "Je bent een pre-trade validator voor een BTC perpetual futures scalper op Hyperliquid. "
        "De scalper gebruikt kwantitatieve signalen (OFI >0.55=bullish, OBI >0.60=bullish, "
        "MOM >0=bullish, CVD >0=bullish). Blokkeer alleen bij een duidelijke narratieve reden "
        "waarom de richting waarschijnlijk fout is. Standaard is GO."
    )
    user = (
        f"Coin: {coin} | Richting: {direction} | Score: {score:.2f} | Regime: {regime}\n"
        f"OFI={ofi} OBI={obi} MOM={mom} CVD={cvd}\n"
        f"Prijs: ${price:,.0f} | Support: {sup_str} | Weerstand: {res_str}\n\n"
        f'Antwoord uitsluitend als JSON: {{"verdict":"GO"|"HOLD"|"BLOCK",'
        f'"confidence":0.0-1.0,"reason":"max 2 zinnen"}}'
    )

    verdict = "go"
    try:
        async with _httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "content-type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-haiku-4-5",
                    "max_tokens": 120,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
            )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"]
        m = _re.search(r'\{[^{}]+\}', text, _re.DOTALL)
        if m:
            data = _json.loads(m.group())
            word = str(data.get("verdict", "GO")).upper()
            if word in ("GO", "HOLD", "BLOCK"):
                verdict = word.lower()
            confidence = float(data.get("confidence", 1.0))
            reason = data.get("reason", "")
            log.info("hl_claude_filter", coin=coin, direction=direction,
                     score=round(score, 3), verdict=verdict,
                     confidence=round(confidence, 2), reason=reason)
    except Exception as exc:
        log.info("hl_claude_filter_error", coin=coin, error=str(exc)[:100],
                 fallback="go")

    _claude_cache[cache_key] = (verdict, time.time())
    return verdict


# ── Signal helpers ─────────────────────────────────────────────────────────────

def _get_signal(coin: str) -> tuple[str | None, float, dict, float | None, list, list]:
    """Haal stoplicht-signaal op uit indicator_engine cache.

    Retourneert (direction, score, signals, price, supports, resistances).
    direction: "UP" | "DOWN" | None
    """
    cache = get_cache(coin)
    direction_eng, score, signals = compute_direction(cache)
    price, supports, resistances = compile_levels(cache)

    direction = None
    if direction_eng == "up":
        direction = "UP"
    elif direction_eng == "down":
        direction = "DOWN"

    return direction, score, signals, (price if price and price > 0 else None), supports, resistances


def _is_confirmed(direction: str, signals: dict) -> bool:
    """OFI én OBI bevestigen de richting — vervangt Polymarket 'confirmed'."""
    ofi = signals.get("spot_ofi")
    obi = signals.get("obi")
    if ofi is None or obi is None:
        return False
    if direction == "UP":
        return ofi > 0.55 and obi > 0.60
    return ofi < 0.45 and obi < 0.40


# ── Trail stop ─────────────────────────────────────────────────────────────────

def _tick_trail(pos: HLPosition, current: float,
                activate_pct: float, buffer_pct: float) -> bool:
    """Werk trailing bij. Retourneert True als trail stop moet vuren."""
    if pos.direction == "LONG":
        if current > pos.peak_price:
            pos.peak_price = current
        if (not pos.trailing_active
                and pos.peak_price >= pos.entry_price * (1 + activate_pct / 100)):
            pos.trailing_active = True
            log.info("hl_trail_activated", direction="LONG",
                     peak=round(pos.peak_price, 2), entry=round(pos.entry_price, 2))
        return (pos.trailing_active
                and current <= pos.peak_price * (1 - buffer_pct / 100))
    else:  # SHORT
        if current < pos.peak_price:
            pos.peak_price = current
        if (not pos.trailing_active
                and pos.peak_price <= pos.entry_price * (1 - activate_pct / 100)):
            pos.trailing_active = True
            log.info("hl_trail_activated", direction="SHORT",
                     peak=round(pos.peak_price, 2), entry=round(pos.entry_price, 2))
        return (pos.trailing_active
                and current >= pos.peak_price * (1 + buffer_pct / 100))


# ── PnL berekening ─────────────────────────────────────────────────────────────

def _calc_pnl_usdc(pos: HLPosition, exit_price: float) -> float:
    leverage = _cfg().get("leverage", 5)
    notional = pos.size_usdc * leverage
    if pos.direction == "LONG":
        return notional * (exit_price - pos.entry_price) / pos.entry_price
    return notional * (pos.entry_price - exit_price) / pos.entry_price


# ── Volatiliteits-regime sizing ─────────────────────────────────────────────────
# Enige factor die de hele edge-hunt statistisch overleefde (ANOVA p=1.7e-8):
# BTC beweegt in het weekend ~27% minder dan op weekdagen (za/zo ~2.2% vs ~3.0%).
# Minder beweging = minder scalp-kans + dunnere orderbooks → kleiner inzetten.
# Geen richtingvoorspelling; puur inzet-grootte koppelen aan wanneer er iets valt te halen.

def _vol_regime_multiplier() -> tuple[float, str]:
    """Sizing-multiplier op basis van dag-van-week (UTC). Retourneert (mult, label)."""
    sz = _cfg().get("sizing", {})
    if not sz.get("vol_sizing_enabled", True):
        return 1.0, "uit"
    weekend_mult = sz.get("weekend_multiplier", 0.6)  # ~vol-ratio, iets conservatiever
    dow = datetime.now(timezone.utc).weekday()  # 0=maandag … 5=za, 6=zo
    if dow >= 5:
        return weekend_mult, "weekend"
    return 1.0, "weekdag"


# ── Entry / exit ───────────────────────────────────────────────────────────────

async def _enter(coin: str, direction: str, current_price: float, paper: bool) -> bool:
    """Open nieuwe positie. Retourneert True bij succes."""
    cfg = _cfg()
    leverage = cfg.get("leverage", 5)
    main_pct = cfg.get("sizing", {}).get("main_pct_per_100", 5.0)
    vol_mult, vol_label = _vol_regime_multiplier()
    is_long = direction == "UP"

    if paper:
        slippage = 0.0005  # 0.05% simulatie-slippage
        fill = current_price * (1 + slippage if is_long else 1 - slippage)
        size_usdc = cfg.get("paper_size_usdc", 10.0) * vol_mult
        size_coin = round((size_usdc * leverage) / fill, 6)
    else:
        from . import hyperliquid_orders as _hl
        balance = _hl.get_balance()
        if balance < 5.0:
            log.warning("hl_balance_too_low", balance=round(balance, 2))
            return False
        size_usdc = balance * main_pct / 100 * vol_mult
        size_coin = round((size_usdc * leverage) / current_price, 6)
        min_size = cfg.get("min_order_size_coin", 0.001)
        if size_coin < min_size:
            log.warning("hl_size_too_small", size_coin=size_coin, min=min_size)
            return False
        fill, filled_size = _hl.place_market_open(coin, is_long, size_coin, leverage)
        if fill <= 0:
            return False
        size_coin = filled_size

    pos = HLPosition(
        direction=direction,
        entry_price=fill if not paper else current_price,
        size_usdc=size_usdc,
        size_coin=size_coin,
        peak_price=current_price,
    )
    _positions[coin] = pos
    log.info("hl_entry", coin=coin, direction=direction,
             price=round(pos.entry_price, 2), size_usdc=round(size_usdc, 2),
             size_coin=size_coin, leverage=leverage,
             vol_regime=vol_label, vol_mult=vol_mult, paper=paper)
    return True


async def _exit(coin: str, reason: str, current_price: float, paper: bool) -> None:
    """Sluit huidige positie."""
    pos = _positions.get(coin)
    if not pos:
        return

    if paper:
        slippage = 0.0005
        fill = current_price * (1 - slippage if pos.direction == "LONG" else 1 + slippage)
    else:
        from . import hyperliquid_orders as _hl
        fill, _ = _hl.place_market_close(coin)
        if fill <= 0:
            log.error("hl_exit_failed", coin=coin, reason=reason)
            return

    pnl = _calc_pnl_usdc(pos, fill)
    held_mins = (time.time() - pos.opened_at) / 60
    log.info("hl_exit", coin=coin, direction=pos.direction, reason=reason,
             entry=round(pos.entry_price, 2), exit_price=round(fill, 2),
             pnl_usdc=round(pnl, 4), pnl_pct=round(pos.pnl_pct(fill), 3),
             held_mins=round(held_mins, 1), paper=paper)
    _positions[coin] = None


# ── Hoofd-tick ─────────────────────────────────────────────────────────────────

async def _tick(coin: str, paper: bool) -> None:
    """Één trading-tick: evalueer signaal en beheer positie."""
    cfg = _cfg()
    entry_score_min  = cfg.get("entry_score_min", 0.65)
    green_threshold  = cfg.get("green_threshold", 0.60)
    trail_activate   = cfg.get("trail_activate_pct", 0.40)   # % gain om trailing te activeren
    trail_buffer     = cfg.get("trail_buffer_pct", 0.20)     # % terug van piek = stop
    profit_target    = cfg.get("profit_target_pct", 0.50)    # % gain = direct sluiten
    mom_rev_score    = cfg.get("mom_reversal_score", 0.40)   # minimumscore voor MOM-reversal

    direction, score, signals, price, supports, resistances = _get_signal(coin)
    regime = signals.get("regime", "UNKNOWN") if signals else "UNKNOWN"

    if price is None:
        log.info("hl_tick_no_data", coin=coin,
                 hint="wacht op Kraken-data (~20s na start)")
        return

    pos = _positions.get(coin)
    consensus = score >= green_threshold and direction is not None

    # ── Beheer open positie ───────────────────────────────────────────────────
    if pos:
        should_stop = _tick_trail(pos, price, trail_activate, trail_buffer)

        # Profit target
        if profit_target > 0 and pos.pnl_pct(price) >= profit_target:
            await _exit(coin, "profit_target", price, paper)
            return

        # MOM-reversal: signaal draait met voldoende overtuiging
        opp = "DOWN" if pos.direction == "LONG" else "UP"
        if direction == opp and score >= mom_rev_score:
            # Houd vast als trailing nooit actief was én positie staat al op verlies
            if not pos.trailing_active and pos.pnl_pct(price) < 0:
                log.info("hl_mom_reversal_hold", coin=coin,
                         price=round(price, 2), entry=round(pos.entry_price, 2),
                         pnl_pct=round(pos.pnl_pct(price), 3))
                return
            await _exit(coin, "mom_reversal", price, paper)
            return

        # Trail stop
        if should_stop:
            await _exit(coin, "trail_stop", price, paper)
            return

        # Positiestatus elke ~60s
        if int(time.time()) % 60 < 10:
            log.info("hl_position_status", coin=coin, direction=pos.direction,
                     price=round(price, 2), pnl_pct=round(pos.pnl_pct(price), 3),
                     trailing=pos.trailing_active)

    # ── Open nieuwe positie ───────────────────────────────────────────────────
    elif consensus and score >= entry_score_min and direction:
        confirmed = _is_confirmed(direction, signals)
        if confirmed:
            # Claude pre-entry filter: blokkeert bij narratieve mismatch
            cf = await _claude_filter(
                coin, direction, score, price,
                signals, supports, resistances, regime,
            )
            if cf == "block":
                log.info("hl_entry_claude_blocked", coin=coin,
                         direction=direction, score=round(score, 3))
            else:
                await _enter(coin, direction, price, paper)
        else:
            log.info("hl_entry_unconfirmed", coin=coin, direction=direction,
                     score=round(score, 3),
                     ofi=round(signals.get("spot_ofi") or 0, 3),
                     obi=round(signals.get("obi") or 0, 3))
    else:
        # Geen consensus — log elke ~30s zodat de gebruiker weet dat het draait
        if int(time.time()) % 30 < 10:
            log.info("hl_tick_waiting", coin=coin,
                     direction=direction or "undecided",
                     score=round(score, 3),
                     price=round(price, 2))


# ── Hoofd-loop ─────────────────────────────────────────────────────────────────

async def hl_scalper_loop(coin: str = "BTC", paper: bool = True) -> None:
    """Perpetual trading loop. Draait oneindig."""
    cfg = _cfg()
    poll_secs = cfg.get("poll_interval_secs", 10)

    # Positieherstel na herstart (live mode)
    if not paper:
        try:
            from . import hyperliquid_orders as _hl
            live = _hl.get_open_position(coin)
            if live and live["entry_price"] > 0:
                direction = "LONG" if live["is_long"] else "SHORT"
                ep = live["entry_price"]
                sz = abs(live["size"])
                size_usdc = sz * ep / max(cfg.get("leverage", 5), 1)
                _positions[coin] = HLPosition(
                    direction=direction, entry_price=ep,
                    size_usdc=size_usdc, size_coin=sz, peak_price=ep,
                )
                log.info("hl_position_recovered", coin=coin, direction=direction,
                         entry=round(ep, 2), size_coin=sz)
        except Exception as exc:
            log.warning("hl_position_recovery_failed", coin=coin, error=str(exc)[:120])

    log.info("hl_scalper_started", coin=coin, paper=paper,
             mode="PAPER" if paper else "LIVE *** ECHTE ORDERS ***")

    while True:
        try:
            await _tick(coin, paper)
        except Exception as exc:
            log.error("hl_tick_error", coin=coin, error=str(exc)[:200])
        await asyncio.sleep(poll_secs)


async def hl_multi_loop(paper: bool = True) -> None:
    """Start loops voor alle geconfigureerde coins tegelijk."""
    cfg = _cfg()
    coins_cfg = cfg.get("coins", {"BTC": {"enabled": True}})
    enabled = [c for c, v in coins_cfg.items() if v.get("enabled", False)]
    if not enabled:
        log.warning("hl_no_coins_enabled")
        return
    log.info("hl_multi_start", coins=enabled, paper=paper)
    await asyncio.gather(*(hl_scalper_loop(c, paper) for c in enabled))
