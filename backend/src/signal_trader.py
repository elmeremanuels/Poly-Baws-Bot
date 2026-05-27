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
    get_mode, get_active_trades, get_active_count_by_coin,
    create_trade_state, add_active_trade, remove_active_trade,
    update_trade_field, persist_trade, has_traded_window,
)


def _cfg() -> dict:
    return CONFIG.get("signal_trader", {})


def _coin_enabled(coin: str) -> bool:
    return _cfg().get("coins", {}).get(coin, {}).get("enabled", True)


def _is_paper() -> bool:
    return _cfg().get("paper_mode", True)


def _in_skip_hours(window_start) -> bool:
    """Zelfde uur-filter als de straddle strategie."""
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
    detecteert zulke trades en start de watcher opnieuw.
    """
    now = datetime.now(timezone.utc)
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

        # Alleen als het window al voorbij is (trades die nog lopen, laten we met rust)
        if window_end > now:
            continue

        side       = trade.get("winner_side", "YES")
        buy_token  = (trade.get("condition_id_yes") if side == "YES"
                      else trade.get("condition_id_no"))
        fill_price = (trade.get("entry_yes_price") if side == "YES"
                      else trade.get("entry_no_price")) or 0.50
        size       = float(trade.get("entry_size") or 0.0)
        fees       = float(trade.get("fees_paid") or 0.0)

        # Bouw een mini market-dict met alleen window_end (genoeg voor _wait_resolution)
        mini_market = {"window_end": window_end}

        asyncio.create_task(
            _wait_resolution(trade_id, buy_token, side, mini_market,
                             fill_price, size, fees)
        )
        recovered += 1
        log.info("signal_trade_recovered", trade_id=trade_id, coin=coin,
                 window_end=window_end.isoformat())

    if recovered:
        log.info("signal_recovery_done", coin=coin, recovered=recovered)


async def signal_trader_loop(coin: str) -> None:
    """Per-coin loop. Self-gates op signal_trader mode."""
    # Wacht kort zodat recover_state() klaar is, herstel daarna stuck trades
    await asyncio.sleep(20)
    await _recover_stuck_trades(coin)

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
            log.error("signal_trader_error", coin=coin, error=str(e))
        await asyncio.sleep(10)


async def _check_and_trade(coin: str) -> None:
    cfg = _cfg()
    if not _coin_enabled(coin):
        return

    # Concurrent positie-limiet (eigen config, niet de straddle coin-max)
    active_counts = get_active_count_by_coin()
    total_active = sum(active_counts.values())
    max_concurrent = cfg.get("max_concurrent_positions", 10)
    if total_active >= max_concurrent:
        return

    # Basis risico-check (kill switch, dagelijks verlies, etc.)
    # Gebruik mode="signal_trader_live" zodat daily loss limit WEL actief is
    # maar coin_position_limit gebaseerd is op de straddle config (gaat mis)
    # Doe het handmatig:
    if risk.is_killed():
        return

    # Zoek tradeable market
    market = scanner.get_tradeable_market(coin)
    if not market:
        return

    # Skip uren check
    if _in_skip_hours(market.get("window_start")):
        return

    window_ts = market["window_start"].isoformat() if market["window_start"] else ""
    if has_traded_window(coin, window_ts):
        return

    # Signaal ophalen
    direction, score = _signals.get_conviction(coin)
    threshold = cfg.get("conviction_threshold", 0.65)

    if direction is None or score < threshold:
        return  # Geen/te weinig signaal — skip

    # Converteer UP/DOWN naar YES/NO
    side = "YES" if direction == "UP" else "NO"
    buy_token = market["yes_token"] if side == "YES" else market["no_token"]

    # Entry prijs check
    best_ask = ws_client.get_best_ask(buy_token)
    max_price = cfg.get("entry_price_max", 0.55)
    if best_ask is None or best_ask > max_price:
        log.info("signal_trader_price_rejected", coin=coin,
                 ask=best_ask, max=max_price, direction=direction)
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


async def _do_wait_resolution(
    trade_id: str,
    buy_token: str,
    side: str,
    market: dict,
    fill_price: float,
    size: float,
    fees: float,
) -> None:
    """Interne implementatie van resolution-watcher (gecalled vanuit _wait_resolution)."""
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

    # Uitkomst bepalen via mid-price (na settlement: 1.0 of 0.0)
    # Retry tot maximaal 8 pogingen want settlement kan iets vertraagd zijn
    mid = None
    for attempt in range(8):
        mid = ws_client.get_mid_price(buy_token)
        if mid is not None and (mid >= 0.9 or mid <= 0.1):
            break  # Duidelijk resultaat
        await asyncio.sleep(5)

    # Als WS-prijs niet beschikbaar is, gebruik Signal Lab (straddle) als backup
    if mid is None:
        from .db_sync import get_market_resolution as _get_mkt_res
        trade_now  = get_active_trades().get(trade_id, {})
        market_id  = trade_now.get("market_id")
        sl_winner  = _get_mkt_res(market_id) if market_id else None

        if sl_winner is not None:
            # Straddle-data heeft de uitkomst — gebruik die
            mid = 1.0 if sl_winner == side else 0.0
            log.info("signal_resolution_from_straddle", trade_id=trade_id,
                     coin=coin, sl_winner=sl_winner, side=side)
        else:
            # Geen enkel databron — markeer als aborted
            log.warning("signal_mid_unavailable", trade_id=trade_id, coin=coin,
                        side=side, buy_token=buy_token)
            update_trade_field(trade_id, "status", "aborted")
            update_trade_field(trade_id, "notes",  "resolution_mid_unavailable")
            await persist_trade(trade_id)
            remove_active_trade(trade_id)
            return

    won = mid >= 0.5

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
