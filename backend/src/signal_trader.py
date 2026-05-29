"""
Signal Trader — single-side prediction-based trading.

Strategie:
  1. Wacht op een 5-min window
  2. Haal het signaal op: welke kant wint?
  3. Als conviction >= drempel → koop ALLEEN die kant
  4. Houd vast tot resolution (5 min)
  5. Uitkomst: payout $1/share (winst) of $0 (verlies)

Break-even analyse (entry ~52ct):
  winst per share: 1.00 - fill_price
  verlies per share: fill_price
  break-even accuracy: fill_price / (fill_price + (1 - fill_price)) = fill_price
  Bij 52ct → break-even = 52.0%

Voordelen vs straddle:
  - Entry via limit order → 0% maker fee
  - Resolution = on-chain settlement → 0% fee
  - Geen stop loss, geen trailing, geen peg-cross verlies
"""
import asyncio
import json
from datetime import datetime, timezone, timedelta

from . import scanner, ws_client, paper_trader, risk, signals as _signals
from .config_loader import CONFIG
from .logger import log, write_event
from .state import (
    get_mode, get_active_trades,
    create_trade_state, add_active_trade, remove_active_trade,
    update_trade_field, persist_trade, has_traded_window,
)


def _cfg() -> dict:
    return CONFIG.get("signal_trader", {})


def _coin_enabled(coin: str) -> bool:
    return _cfg().get("coins", {}).get(coin, {}).get("enabled", True)


def _is_paper() -> bool:
    return _cfg().get("paper_mode", True)


async def _daily_loss_exceeded() -> bool:
    """True if today's LIVE signal_trader P&L has hit max_daily_loss_eur.

    Measured against signal_trader trades only (not the background paper straddle
    or learning trades), so it gates new entries without tripping the global kill.
    Paper mode is exempt — simulated losses don't count.
    """
    if _is_paper():
        return False
    limit = float(_cfg().get("max_daily_loss_eur", 50.0))
    if limit <= 0:
        return False
    from .logger import get_mode_daily_pnl
    pnl = await get_mode_daily_pnl("signal_trader")
    return pnl <= -limit


def _in_skip_hours(window_start) -> bool:
    """Uur-filter. Signal_trader negeert dit standaard (respect_trading_hours=false)
    omdat de conviction-gate al slechte uren uitfiltert."""
    if not _cfg().get("respect_trading_hours", False):
        return False  # Signal_trader handelt ongeacht het uur
    th_cfg = CONFIG.get("trading_hours", {})
    if not th_cfg.get("enabled", False):
        return False
    skip_hours = th_cfg.get("skip_utc_hours", [])
    if not skip_hours or window_start is None:
        return False
    utc_hour = window_start.hour if hasattr(window_start, "hour") else None
    return utc_hour in skip_hours


async def _recover_stuck_trades(coin: str) -> None:
    """Herstart resolution-watchers voor signal_holding trades die de bot-herstart overleefden.

    Bij een herstart gaan asyncio-tasks verloren. Trades die gevuld waren maar
    nog niet resolved zijn blijven op status='signal_holding'. Deze functie
    detecteert zulke trades en start de watcher opnieuw — zowel voor trades
    waarvan het window al voorbij is ALS voor trades die nog actief zijn.
    """
    recovered = 0
    for trade_id, trade in list(get_active_trades().items()):
        if trade.get("coin") != coin:
            continue
        if trade.get("mode") != "signal_trader":
            continue
        if trade.get("status") != "signal_holding":
            continue

        window_end_ts = trade.get("window_end_ts")
        if not window_end_ts:
            continue
        try:
            window_end = datetime.fromisoformat(str(window_end_ts))
            if window_end.tzinfo is None:
                window_end = window_end.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        side       = trade.get("winner_side", "YES")
        buy_token  = (trade.get("condition_id_yes") if side == "YES"
                      else trade.get("condition_id_no"))
        fill_price = (trade.get("entry_yes_price") if side == "YES"
                      else trade.get("entry_no_price")) or 0.50
        size       = float(trade.get("entry_size") or 0.0)
        fees       = float(trade.get("fees_paid") or 0.0)

        # Bouw een mini market-dict met window_end (genoeg voor _wait_resolution)
        mini_market = {"window_end": window_end}

        now = datetime.now(timezone.utc)
        already_expired = window_end <= now
        asyncio.create_task(
            _wait_resolution(trade_id, buy_token, side, mini_market,
                             fill_price, size, fees)
        )
        recovered += 1
        log.info("signal_trade_recovered", trade_id=trade_id, coin=coin,
                 window_end=window_end.isoformat(),
                 already_expired=already_expired)

    if recovered:
        log.info("signal_recovery_done", coin=coin, recovered=recovered)


async def signal_trader_loop(coin: str) -> None:
    """Per-coin loop. Self-gates op signal_trader mode."""
    # Wacht kort zodat recover_state() klaar is, herstel daarna stuck trades
    await asyncio.sleep(20)
    await _recover_stuck_trades(coin)

    _tick = 0
    while True:
        if get_mode() != "signal_trader":
            await asyncio.sleep(5)
            continue
        if risk.is_killed():
            await asyncio.sleep(5)
            continue
        try:
            await _check_and_trade(coin)
        except Exception as e:
            log.error("signal_trader_error", coin=coin, error=str(e), exc_info=True)
        _tick += 1
        # Elke 60 s een heartbeat — bevestigt dat de loop actief is
        if _tick % 6 == 0:
            cfg = _cfg()
            market = scanner.get_tradeable_market(coin)
            direction, score = _signals.get_conviction(coin)
            log.info(
                "signal_trader_heartbeat", coin=coin,
                mode_active=(get_mode() == "signal_trader"),
                market_found=bool(market),
                conviction_direction=direction,
                conviction_score=round(score, 3),
                threshold=cfg.get("conviction_threshold", 0.35),
                paper=_is_paper(),
            )
        await asyncio.sleep(10)


async def _check_and_trade(coin: str) -> None:
    cfg = _cfg()
    if not _coin_enabled(coin):
        return

    # Concurrent positie-limiet: tel ALLEEN signal_trader-posities, niet de
    # achtergrond paper-straddles (signal_lab_bg) die parallel Signal Lab-data
    # verzamelen — anders vullen die de slots op en blokkeren ze echte entries.
    st_active = sum(1 for t in get_active_trades().values()
                    if t.get("mode") == "signal_trader")
    max_concurrent = cfg.get("max_concurrent_positions", 10)
    if st_active >= max_concurrent:
        log.info("signal_trader_skip", coin=coin, reason="max_concurrent",
                 active=st_active, max=max_concurrent)
        return

    # Basis risico-check (kill switch, dagelijks verlies, etc.)
    if risk.is_killed():
        log.debug("signal_trader_skip", coin=coin, reason="kill_switch")
        return

    # Eigen dag-verlieslimiet: stop nieuwe live entries (pauzeert niet de hele bot)
    if await _daily_loss_exceeded():
        log.info("signal_trader_skip", coin=coin, reason="daily_loss_limit",
                 limit=_cfg().get("max_daily_loss_eur", 50.0))
        return

    # Zoek tradeable market
    market = scanner.get_tradeable_market(coin)
    if not market:
        log.debug("signal_trader_skip", coin=coin, reason="no_tradeable_market")
        return

    # OFI is een 60s-rolling signal — alleen geldig voor de naaste toekomst.
    # Entreer pas als de window binnen signal_entry_window_minutes start, anders
    # voorspelt een OFI van nu niets over een window die 30+ minuten later opent.
    entry_window_max = cfg.get("entry_start_minutes_before_window", 7)
    ws = market.get("window_start")
    if ws:
        mins_to_start = (ws - datetime.now(timezone.utc)).total_seconds() / 60
        if mins_to_start > entry_window_max:
            log.debug("signal_trader_skip", coin=coin, reason="window_too_far",
                      mins_to_start=round(mins_to_start, 1), max=entry_window_max)
            return

    # Skip uren check
    if _in_skip_hours(market.get("window_start")):
        log.debug("signal_trader_skip", coin=coin, reason="skip_hour")
        return

    window_ts = market["window_start"].isoformat() if market["window_start"] else ""
    if has_traded_window(coin, window_ts):
        log.debug("signal_trader_skip", coin=coin, reason="window_already_traded",
                  window=window_ts)
        return

    # OFI verversen zodat de waarde ≤1s oud is op het moment van beslissing
    # (achtergrond-poll loopt elke 10s — zonder dit kan OFI tot 10s verouderd zijn)
    await _signals.refresh_ofi(coin)

    # Signaal ophalen met alle beschikbare context:
    # drift (5min prijsbeweging) + Polymarket depth imbalance worden nu meegewogen
    direction, score = _signals.get_conviction(
        coin,
        yes_token=market.get("yes_token"),
        no_token=market.get("no_token"),
    )
    threshold = cfg.get("conviction_threshold", 0.35)

    # Edge confirmatie: vergelijk Black-Scholes theoretische waarde met de marktprijs.
    # Positieve edge = Polymarket onderprijst de kans → versterkt het signaal.
    # Negatieve edge = Polymarket overprijst → zwakt het signaal af.
    if direction is not None:
        side_tentative = "YES" if direction == "UP" else "NO"
        ask_tentative = ws_client.get_best_ask(
            market["yes_token"] if side_tentative == "YES" else market["no_token"]
        )
        if ask_tentative and ask_tentative > 0:
            edge = _signals.get_edge(coin, side_tentative, ask_tentative, 300.0)
            if edge is not None:
                if edge > 0.05:
                    score = min(1.0, score * 1.20)
                elif edge < -0.03:
                    score = score * 0.80

    # Multi-coin OFI consensus: unanimiteit = echte macro-bevestiging; divergentie = penalty.
    # De vorige ×1.10 bij 4/5 coins was bijna altijd actief (hoge crypto-correlatie).
    # Nu: boost alleen bij unanimiteit (alle coins met OFI zijn aligned); penalty bij divergentie.
    if direction is not None:
        aligned, total = _signals.get_multi_coin_ofi_consensus(direction)
        if total >= 3:
            if aligned == total:
                score = min(1.0, score * 1.10)   # alle gemeten coins bevestigen
            elif aligned < total / 2:
                score = score * 0.85              # meerderheid divergeert → minder betrouwbaar

    # TWAP check: betalen we significant boven het tijdsgewogen gemiddelde?
    # Als ja = we stappen in een micropump → verlaag de score met 10%.
    # Puur kwaliteitsfilter, geen directionele aanpassing.
    if direction is not None:
        yes_token_id = market.get("yes_token")
        if yes_token_id:
            twap = _signals.get_yes_twap(yes_token_id)
            if twap and twap > 0:
                side_tentative = "YES" if direction == "UP" else "NO"
                ask_check = ws_client.get_best_ask(
                    yes_token_id if side_tentative == "YES" else market.get("no_token", "")
                )
                if ask_check and ask_check > twap * 1.05:
                    score = score * 0.90

    log.info("signal_trader_signal", coin=coin,
             direction=direction, score=round(score, 3), threshold=threshold,
             window=window_ts)

    if direction is None or score < threshold:
        log.info("signal_trader_skip", coin=coin, reason="low_conviction",
                 direction=direction, score=round(score, 3), threshold=threshold)
        return

    # Converteer UP/DOWN naar YES/NO
    side = "YES" if direction == "UP" else "NO"
    buy_token = market["yes_token"] if side == "YES" else market["no_token"]

    # Correlated position check — max N open trades in dezelfde richting
    max_corr = CONFIG.get("signal_trader", {}).get("max_correlated_positions",
               CONFIG.get("router", {}).get("max_correlated_positions", 2))
    from .logger import get_active_trades as _get_active
    same_dir = sum(
        1 for t in _get_active().values()
        if t.get("conviction_at_entry") == direction
        and t.get("triggered_by", "").startswith("signal")
    )
    if same_dir >= max_corr:
        log.info("signal_trader_skip", coin=coin, reason="correlated_risk",
                 direction=direction, open_same_dir=same_dir, max=max_corr)
        return

    # Entry prijs check
    best_ask = ws_client.get_best_ask(buy_token)
    max_price = cfg.get("entry_price_max", 0.55)
    if best_ask is None or best_ask > max_price:
        log.info("signal_trader_price_rejected", coin=coin,
                 ask=best_ask, max=max_price, direction=direction, score=round(score, 3))
        return

    await _execute_entry(coin, market, side, buy_token, direction, score, cfg)


async def _execute_entry(
    coin: str, market: dict, side: str, buy_token: str,
    direction: str, score: float, cfg: dict,
) -> None:
    """Koop de voorspelde kant en start resolution watcher."""
    trade_size_eur = cfg.get("trade_size_eur", 10.0)
    best_ask = ws_client.get_best_ask(buy_token)
    if not best_ask or best_ask <= 0:
        return

    # Aantal shares te kopen
    size = round(trade_size_eur / best_ask, 2)

    # Trade state aanmaken — triggered_by onderscheidt paper vs live voor logging
    triggered_by = "signal_paper" if _is_paper() else "signal_live"
    trade = create_trade_state(coin, market, "signal_trader", triggered_by=triggered_by)
    trade_id = trade["trade_id"]

    # Signals stampen
    all_sigs = _signals.get_all_signals(coin)
    trade["ofi_at_entry"]              = all_sigs.get("ofi")
    trade["funding_rate_at_entry"]     = all_sigs.get("funding_rate")
    trade["liq_proxy_at_entry"]        = all_sigs.get("liq_proxy")
    trade["conviction_at_entry"]       = direction  # "UP" of "DOWN"
    trade["conviction_score_at_entry"] = round(score, 3)
    # Signal_trader heeft geen aparte trigger-fase — kopieer naar _at_trigger
    trade["conviction_at_trigger"]       = direction
    trade["conviction_score_at_trigger"] = round(score, 3)
    trade["ofi_at_trigger"]              = all_sigs.get("ofi")
    trade["funding_rate_at_trigger"]     = all_sigs.get("funding_rate")
    trade["liq_proxy_at_trigger"]        = all_sigs.get("liq_proxy")
    trade["bias_direction_at_entry"]     = direction  # Signal Lab: bias = conviction richting

    # Trade metadata
    trade["trigger_hit"]  = True
    trade["trigger_ts"]   = datetime.now(timezone.utc).isoformat()
    trade["winner_side"]  = side  # "YES" of "NO"
    trade["status"]       = "signal_holding"
    trade["param_snapshot"] = json.dumps({
        "conviction": round(score, 3),
        "direction": direction,
        "side": side,
        "entry_ask": best_ask,
        "trade_size_eur": trade_size_eur,
        "paper_mode": _is_paper(),
    })

    add_active_trade(trade)  # registreert ook window in _window_registry

    await write_event(trade_id, "signal_entry_initiated", coin, {
        "direction": direction, "side": side,
        "conviction": round(score, 3), "size": size,
        "ask": best_ask, "paper": _is_paper(),
    })

    # ── Order plaatsen ──────────────────────────────────────────────────────────
    if _is_paper():
        # Paper: simuleer limit buy via live orderbook
        result = await paper_trader.simulate_limit_buy(buy_token, best_ask, size)
        if not result.get("filled"):
            # Fallback naar market buy als limit niet vult
            result = await paper_trader.simulate_market_buy(buy_token, size)
    else:
        # Live: limit order plaatsen bij best ask (maker → 0% fee)
        from . import orders as _orders
        resp = await _orders.place_limit_order(buy_token, "BUY", best_ask, size)
        if not resp or not resp.get("order_id"):
            log.error("signal_trader_order_failed", coin=coin, trade_id=trade_id)
            remove_active_trade(trade_id)
            update_trade_field(trade_id, "status", "aborted")
            update_trade_field(trade_id, "notes", "order_placement_failed")
            await persist_trade(trade_id)
            return

        # Wacht 5s op fill
        await asyncio.sleep(5)
        order = await _orders.get_order(resp["order_id"])
        status = order.get("status") if order else None
        if status not in ("MATCHED", "FILLED"):
            await _orders.cancel_order(resp["order_id"])
            log.info("signal_trader_not_filled", trade_id=trade_id, status=status)
            remove_active_trade(trade_id)
            update_trade_field(trade_id, "status", "aborted")
            update_trade_field(trade_id, "notes", "entry_not_filled")
            await persist_trade(trade_id)
            return

        result = {"filled": True, "fill_price": best_ask, "fees": 0.0}

    if not result.get("filled"):
        log.info("signal_entry_not_filled", trade_id=trade_id, coin=coin)
        remove_active_trade(trade_id)
        update_trade_field(trade_id, "status", "aborted")
        await persist_trade(trade_id)
        return

    fill_price = result.get("fill_price") or best_ask
    fees       = result.get("fees") or 0.0

    if side == "YES":
        update_trade_field(trade_id, "entry_yes_price", fill_price)
    else:
        update_trade_field(trade_id, "entry_no_price", fill_price)
    update_trade_field(trade_id, "entry_size", size)
    update_trade_field(trade_id, "entry_filled_ts", datetime.now(timezone.utc).isoformat())
    update_trade_field(trade_id, "fees_paid", fees)

    await persist_trade(trade_id)

    log.info("signal_entry_filled", trade_id=trade_id, coin=coin,
             direction=direction, side=side, price=fill_price, size=size,
             paper=_is_paper())

    # Start resolution watcher in background
    asyncio.create_task(
        _wait_resolution(trade_id, buy_token, side, market, fill_price, size, fees)
    )


async def _wait_resolution(
    trade_id: str,
    buy_token: str,
    side: str,
    market: dict,
    fill_price: float,
    size: float,
    fees: float,
) -> None:
    """Wacht tot het 5-min window afloopt en verwerk de uitkomst."""
    try:
        await _do_wait_resolution(trade_id, buy_token, side, market,
                                  fill_price, size, fees)
    except Exception as exc:
        # Zorg dat de trade altijd netjes gesloten wordt — ook bij onverwachte fouten
        log.error("signal_resolution_error", trade_id=trade_id,
                  error=str(exc), exc_info=True)
        try:
            update_trade_field(trade_id, "status", "aborted")
            update_trade_field(trade_id, "notes",  f"resolution_error: {exc}")
            await persist_trade(trade_id)
        except Exception:
            pass
        remove_active_trade(trade_id)


async def _get_midpoint_rest(token_id: str) -> float | None:
    """Haal de mid-price op via CLOB REST — werkt ook na settlement (lege orderbook).

    Na resolution laat de CLOB REST API de settlement-prijs zien (1.0 of 0.0).
    De WS-cache is dan leeg (geen bids/asks meer) en geeft None terug.
    """
    import httpx
    clob_url = CONFIG.get("polymarket", {}).get("clob_rest_url",
                                                "https://clob.polymarket.com")
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(f"{clob_url}/midpoint",
                                    params={"token_id": token_id})
        if resp.status_code == 200:
            data = resp.json()
            mid = data.get("mid")
            return float(mid) if mid is not None else None
    except Exception as exc:
        log.debug("midpoint_rest_error", token_id=token_id, error=str(exc))
    return None


async def _do_wait_resolution(
    trade_id: str,
    buy_token: str,
    side: str,
    market: dict,
    fill_price: float,
    size: float,
    fees: float,
) -> None:
    """Interne implementatie van resolution-watcher (gecalled vanuit _wait_resolution).

    Databronnen (in prioriteit):
      1. CLOB REST /midpoint  — betrouwbaar ook na settlement (lege orderbook)
      2. WS orderbook cache   — werkt alleen als er nog bids/asks in de book zitten
      3. Signal Lab (straddle) — DB-backup: actual_winner van zelfde market_id
      4. Abort                 — geen enkele bron heeft een conclusie
    """
    trade = get_active_trades().get(trade_id)
    if not trade:
        return

    coin       = trade["coin"]
    window_end = market.get("window_end")
    if not window_end:
        log.error("signal_no_window_end", trade_id=trade_id)
        return

    # Wacht tot window_end + 8s buffer voor settlement
    now       = datetime.now(timezone.utc)
    wait_secs = (window_end - now).total_seconds() + 8
    if wait_secs > 0:
        await asyncio.sleep(wait_secs)

    mid: float | None = None
    source = "unknown"

    # ── 1. CLOB REST midpoint (primair — betrouwbaar na settlement) ─────────────
    for attempt in range(5):
        mid = await _get_midpoint_rest(buy_token)
        if mid is not None and (mid >= 0.9 or mid <= 0.1):
            source = "clob_rest"
            break
        # Nog niet settled of endpoint traag — kort wachten
        await asyncio.sleep(4)

    # ── 2. WS orderbook cache (fallback als REST nog geen clear resultaat heeft) ─
    if mid is None or (mid is not None and 0.1 < mid < 0.9):
        for attempt in range(4):
            ws_mid = ws_client.get_mid_price(buy_token)
            if ws_mid is not None and (ws_mid >= 0.9 or ws_mid <= 0.1):
                mid = ws_mid
                source = "ws_cache"
                break
            await asyncio.sleep(3)

    # ── 3. Signal Lab straddle-data (DB backup voor zelfde market) ───────────────
    if mid is None or (mid is not None and 0.1 < mid < 0.9):
        from .db_sync import get_market_resolution as _get_mkt_res
        trade_now = get_active_trades().get(trade_id, {})
        market_id = trade_now.get("market_id")
        sl_winner = _get_mkt_res(market_id) if market_id else None
        if sl_winner is not None:
            mid    = 1.0 if sl_winner == side else 0.0
            source = "signal_lab"
            log.info("signal_resolution_from_straddle", trade_id=trade_id,
                     coin=coin, sl_winner=sl_winner, side=side)

    # ── 4. Abort als alle bronnen falen ─────────────────────────────────────────
    if mid is None:
        log.warning("signal_mid_unavailable", trade_id=trade_id, coin=coin,
                    side=side, buy_token=buy_token)
        update_trade_field(trade_id, "status", "aborted")
        update_trade_field(trade_id, "notes",  "resolution_mid_unavailable")
        await persist_trade(trade_id)
        remove_active_trade(trade_id)
        return

    # Mid beschikbaar maar niet conclusief (tussen 0.1 en 0.9) — gebruik > 0.5
    won = mid >= 0.5
    log.info("signal_resolution_source", trade_id=trade_id, source=source,
             mid=round(mid, 4), won=won)

    if won:
        gross_pnl     = (1.0 - fill_price) * size
        actual_winner = side
        exit_reason   = "resolution_won"
        exit_price    = 1.0
    else:
        gross_pnl     = -fill_price * size
        actual_winner = "NO" if side == "YES" else "YES"
        exit_reason   = "resolution_lost"
        exit_price    = 0.0

    net_pnl = gross_pnl - fees

    update_trade_field(trade_id, "actual_winner",      actual_winner)
    update_trade_field(trade_id, "winner_exit_price",  exit_price)
    update_trade_field(trade_id, "winner_exit_reason", exit_reason)
    update_trade_field(trade_id, "winner_exit_ts",     datetime.now(timezone.utc).isoformat())
    update_trade_field(trade_id, "gross_pnl",          round(gross_pnl, 4))
    update_trade_field(trade_id, "net_pnl",            round(net_pnl, 4))
    update_trade_field(trade_id, "status",             "resolved")
    update_trade_field(trade_id, "mid_at_trigger",     mid)

    await persist_trade(trade_id)
    remove_active_trade(trade_id)

    await write_event(trade_id, "signal_resolved", coin, {
        "won": won, "side": side,
        "actual_winner": actual_winner,
        "net_pnl": round(net_pnl, 4),
        "mid_at_resolution": mid,
    })

    emoji = "✅" if won else "❌"
    log.info("signal_resolved", trade_id=trade_id, coin=coin,
             result=emoji, side=side,
             net_pnl=round(net_pnl, 4), mid=mid)
