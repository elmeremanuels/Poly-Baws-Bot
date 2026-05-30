"""Live orderbook monitoring and trigger detection for active trades."""
import asyncio
from datetime import datetime, timezone, timedelta

from . import orders, ws_client, paper_trader
from .config_loader import CONFIG
from .logger import log, write_event, write_snapshot
from .state import get_active_trades, update_trade_field, persist_trade

_DEFAULT_TRIGGER_THRESHOLD = CONFIG["trading"]["trigger_threshold"]
_monitoring_tasks: dict[str, asyncio.Task] = {}
_early_sell_in_progress: set[str] = set()   # trade_ids with an early sell task running
_rebuy_in_progress: set[str] = set()        # trade_ids with a rebuy task running
_save_it_state: dict[str, dict] = {}        # trade_id -> {peak_yes, peak_no, executed}
_save_it_in_progress: set[str] = set()      # trade_ids with a save_it task running
# Duration tracking for early loser: loop.time() when loser first fell below threshold.
# Reset when price recovers. Used to prevent selling on temporary dips.
_loser_below_ts: dict[str, float] = {}      # trade_id → loop.time()
_bggdsb_hedge_in_progress: set[str] = set()  # trade_ids with a hedge task running


def _get_early_thresholds(regime: str) -> tuple[float | None, float | None]:
    """Return (early_loser_threshold, rebuy_threshold) for the given regime.

    Regime-specific config overrides the global exit defaults.
    Returns (None, None) if early loser sell is disabled for this regime.
    """
    profiles = CONFIG.get("regime_profiles", {})
    regime_cfg = profiles.get(regime, {})
    exit_cfg = CONFIG.get("exit", {})

    raw_thr = regime_cfg.get("early_loser_threshold", exit_cfg.get("early_loser_threshold"))
    raw_rebuy = regime_cfg.get("early_loser_rebuy_threshold", exit_cfg.get("early_loser_rebuy_threshold"))

    if raw_thr is None:
        return None, None
    return float(raw_thr), float(raw_rebuy) if raw_rebuy is not None else None


async def start_monitoring(trade_id: str, on_trigger_callback) -> None:
    """Start monitoring an active trade for trigger condition."""
    if trade_id in _monitoring_tasks and not _monitoring_tasks[trade_id].done():
        return
    task = asyncio.create_task(_monitor_trade(trade_id, on_trigger_callback))
    _monitoring_tasks[trade_id] = task
    log.info("monitor_started", trade_id=trade_id)

    # Oracle active monitor — watches temperature during straddle trade
    from .config_loader import CONFIG as _CFG
    if _CFG.get("oracle", {}).get("enabled", False):
        from .state import get_active_trades as _gat
        trade = _gat().get(trade_id, {})
        window_end = trade.get("window_end_ts")
        if window_end:
            from datetime import datetime, timezone
            try:
                we = datetime.fromisoformat(str(window_end))
                if we.tzinfo is None:
                    we = we.replace(tzinfo=timezone.utc)
                from . import oracle as _oracle_mod
                asyncio.create_task(_oracle_mod.watch_trade_oracle(
                    trade_id=trade_id,
                    coin=trade.get("coin", "UNKNOWN"),
                    conviction_score=float(trade.get("conviction_score_at_entry") or 0.5),
                    regime=str(trade.get("regime") or "UNKNOWN"),
                    window_end=we,
                ))
            except Exception:
                pass


async def stop_monitoring(trade_id: str) -> None:
    task = _monitoring_tasks.pop(trade_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    _loser_below_ts.pop(trade_id, None)
    _save_it_state.pop(trade_id, None)
    log.info("monitor_stopped", trade_id=trade_id)


async def _sell_early_loser_task(
    trade_id: str,
    loser_side: str,
    loser_token: str,
    loser_size: float,
    paper: bool,
    coin: str,
) -> None:
    """Market-sell the presumed loser early during monitoring phase."""
    try:
        if paper:
            result = await paper_trader.simulate_market_sell(loser_token, loser_size)
            fill_price = result.get("fill_price") or 0.30
        else:
            mid = ws_client.get_mid_price(loser_token) or 0.30
            fallback = round(mid * 0.7, 2)
            resp = await orders.place_market_order(loser_token, "SELL", loser_size)
            fill_price = fallback
            if resp and resp.get("order_id"):
                for _ in range(3):
                    await asyncio.sleep(1.0)
                    order = await orders.get_order(resp["order_id"])
                    if order and order.get("status") in ("MATCHED", "FILLED"):
                        fill_price = float(order.get("average_price") or order.get("price") or fallback)
                        break

        update_trade_field(trade_id, "early_loser_side", loser_side)
        update_trade_field(trade_id, "early_loser_price", fill_price)
        update_trade_field(trade_id, "early_loser_ts", datetime.now(timezone.utc).isoformat())
        await persist_trade(trade_id)

        log.info("early_loser_sold", trade_id=trade_id, coin=coin,
                 side=loser_side, fill_price=round(fill_price, 4), paper=paper)
        await write_event(trade_id, "early_loser_sold", coin,
                          {"side": loser_side, "fill_price": fill_price})
    except Exception as exc:
        log.error("early_loser_sell_error", trade_id=trade_id, error=str(exc))
    finally:
        _early_sell_in_progress.discard(trade_id)


async def _rebuy_early_sold_task(
    trade_id: str,
    sold_side: str,
    sold_token: str,
    sold_size: float,
    paper: bool,
    coin: str,
) -> None:
    """Re-buy the early-sold side when it recovers — safeguard for wrong-side sells."""
    try:
        if paper:
            ask = ws_client.get_best_ask(sold_token) or 0.55
            result = await paper_trader.simulate_limit_buy(sold_token, ask, sold_size)
            fill_price = result.get("fill_price") or ask
        else:
            ask = round(ws_client.get_best_ask(sold_token) or 0.55, 2)
            resp = await orders.place_limit_order(sold_token, "BUY", ask, sold_size)
            fill_price = ask  # limit buy at ask = immediate taker fill

        update_trade_field(trade_id, "early_loser_rebought", True)
        await persist_trade(trade_id)

        log.info("early_loser_rebought", trade_id=trade_id, coin=coin,
                 side=sold_side, fill_price=round(fill_price, 4))
        await write_event(trade_id, "early_loser_rebought", coin,
                          {"side": sold_side, "fill_price": fill_price})
    except Exception as exc:
        log.error("early_loser_rebuy_error", trade_id=trade_id, error=str(exc))
    finally:
        _rebuy_in_progress.discard(trade_id)


async def _save_it_task(
    trade_id: str, side_to_buy: str, token_id: str, save_shares: float,
    is_paper: bool, coin: str,
) -> None:
    """Buy the reversing side to hedge a potential reversal before trigger fires."""
    try:
        if is_paper:
            result = await paper_trader.simulate_market_buy(token_id, save_shares)
        else:
            resp = await orders.place_market_order(token_id, "BUY", save_shares)
            if resp and resp.get("order_id"):
                f = await _poll_live_fill_for_save_it(resp["order_id"], fallback=0.50, size=save_shares)
                result = {"filled": True, "fill_price": f["fill_price"], "fees": f["fees"]}
            else:
                result = None

        if result and result.get("filled"):
            fill_price = result.get("fill_price") or 0.50
            update_trade_field(trade_id, "save_it_executed", 1)
            update_trade_field(trade_id, "save_it_side", side_to_buy)
            log.info("save_it_executed", trade_id=trade_id, coin=coin,
                     side=side_to_buy, fill_price=round(fill_price, 4),
                     save_shares=save_shares, paper=is_paper)
            await write_event(trade_id, "save_it_executed", coin, {
                "side": side_to_buy, "fill_price": round(fill_price, 4),
                "save_shares": save_shares,
            })
        else:
            log.warning("save_it_fill_failed", trade_id=trade_id, side=side_to_buy)
    except Exception as e:
        log.error("save_it_task_error", trade_id=trade_id, error=str(e))
    finally:
        _save_it_in_progress.discard(trade_id)


async def _bggdsb_hedge_task(
    trade_id: str, dominant_side: str, hedge_token: str, hedge_shares: float,
    is_paper: bool, coin: str,
) -> None:
    """Buy the opposing side as a late hedge when price drops to ≤0.15."""
    try:
        if is_paper:
            result = await paper_trader.simulate_market_buy(hedge_token, hedge_shares)
        else:
            resp = await orders.place_market_order(hedge_token, "BUY", hedge_shares)
            if resp and resp.get("order_id"):
                f = await _poll_live_fill_for_save_it(resp["order_id"], fallback=0.10, size=hedge_shares)
                result = {"filled": True, "fill_price": f["fill_price"], "fees": f["fees"]}
            else:
                result = None

        if result and result.get("filled"):
            fill_price = result.get("fill_price") or 0.10
            hedge_side = "NO" if dominant_side == "YES" else "YES"
            update_trade_field(trade_id, "bggdsb_hedge_placed", True)
            log.info("bggdsb_hedge_executed", trade_id=trade_id, coin=coin,
                     hedge_side=hedge_side, fill_price=round(fill_price, 4),
                     hedge_shares=hedge_shares, paper=is_paper)
            await write_event(trade_id, "bggdsb_hedge_executed", coin, {
                "hedge_side": hedge_side, "fill_price": round(fill_price, 4),
                "hedge_shares": hedge_shares,
            })
        else:
            log.warning("bggdsb_hedge_fill_failed", trade_id=trade_id)
    except Exception as e:
        log.error("bggdsb_hedge_task_error", trade_id=trade_id, error=str(e))
    finally:
        _bggdsb_hedge_in_progress.discard(trade_id)


async def _poll_live_fill_for_save_it(order_id: str, fallback: float, size: float) -> dict:
    """Wait briefly for a live market buy fill (for save_it orders)."""
    for _ in range(8):
        await asyncio.sleep(1.0)
        try:
            order = await orders.get_order(order_id)
            if order:
                status = order.get("status")
                if status in ("MATCHED", "FILLED"):
                    avg = float(order.get("average_price") or fallback)
                    return {"fill_price": avg, "fees": avg * size * 0.018}
                if status in ("CANCELED", "UNMATCHED"):
                    break
        except Exception:
            pass
    return {"fill_price": fallback, "fees": fallback * size * 0.018}


async def _monitor_trade(trade_id: str, on_trigger_callback) -> None:
    trades = get_active_trades()
    trade = trades.get(trade_id)
    if not trade:
        log.error("monitor_trade_not_found", trade_id=trade_id)
        return

    yes_token = trade.get("condition_id_yes")
    no_token = trade.get("condition_id_no")
    window_end = trade.get("window_end_ts")

    if window_end:
        from datetime import datetime, timezone
        window_end_dt = datetime.fromisoformat(window_end).astimezone(timezone.utc)
    else:
        window_end_dt = None

    snapshot_interval = 30  # seconds between snapshots
    last_snapshot = 0.0
    loop = asyncio.get_event_loop()
    monitor_start = loop.time()  # track monitoring age for early loser cooldown

    while True:
        now = datetime.now(timezone.utc)
        if window_end_dt and now >= window_end_dt:
            log.info("monitor_window_expired", trade_id=trade_id)
            update_trade_field(trade_id, "winner_exit_reason", "resolution")
            yes_mid_final = ws_client.get_mid_price(yes_token)
            if yes_mid_final is not None:
                update_trade_field(trade_id, "actual_winner", "YES" if yes_mid_final >= 0.5 else "NO")
            await on_trigger_callback(trade_id, "RESOLUTION", None)
            break

        # Don't fire trigger before window start — early entry (T-45min) may have
        # pre-window price swings that don't reflect the actual window dynamics.
        window_start_ts = trade.get("window_start_ts")
        if window_start_ts:
            ws_dt = datetime.fromisoformat(window_start_ts).astimezone(timezone.utc)
            if now < ws_dt - timedelta(seconds=30):
                await asyncio.sleep(1.0)
                continue

        coin = trade.get("coin", "")
        from . import regime as _regime
        threshold = _regime.get_effective_trigger_threshold(coin)
        winner, price = paper_trader.check_trigger(yes_token, no_token, threshold)
        if winner:
            log.info("trigger_detected", trade_id=trade_id, winner=winner, price=price)
            update_trade_field(trade_id, "trigger_hit", True)
            update_trade_field(trade_id, "trigger_ts", now.isoformat())
            update_trade_field(trade_id, "winner_side", winner)
            await write_event(trade_id, "trigger", trade["coin"], {"winner": winner, "price": price})

            # Collect trigger-time data for learning analysis
            winner_token = yes_token if winner == "YES" else no_token
            from . import volatility as _vol
            mid_trig = ws_client.get_mid_price(winner_token)
            bid_trig = ws_client.get_best_bid(winner_token)
            ask_trig = ws_client.get_best_ask(winner_token)
            spread_trig = round(ask_trig - bid_trig, 4) if (bid_trig is not None and ask_trig is not None) else None
            vel = _vol.get_price_velocity(winner_token)
            # Depth at best bid: size available at top-of-book
            y_book = ws_client.get_orderbook(yes_token)
            n_book = ws_client.get_orderbook(no_token)

            def _top_bid_depth(book: dict) -> float | None:
                bids = book.get("bids", {})
                if not bids:
                    return None
                best = str(max(float(p) for p in bids.keys()))
                return bids.get(best, 0)

            y_depth = _top_bid_depth(y_book)
            n_depth = _top_bid_depth(n_book)
            # Time since window start
            ws_ts = trade.get("window_start_ts")
            if ws_ts:
                ws_dt = datetime.fromisoformat(ws_ts).astimezone(timezone.utc)
                time_since_start = round((now - ws_dt).total_seconds(), 1)
            else:
                time_since_start = None
            update_trade_field(trade_id, "mid_at_trigger", mid_trig)
            update_trade_field(trade_id, "spread_at_trigger", spread_trig)
            update_trade_field(trade_id, "mid_velocity_at_trigger", vel)
            update_trade_field(trade_id, "yes_depth_at_trigger", y_depth)
            update_trade_field(trade_id, "no_depth_at_trigger", n_depth)
            update_trade_field(trade_id, "time_since_window_start", time_since_start)

            # Phase 1: stamp signals at trigger time — refresh first so data is ≤1s old
            from . import signals as _sig
            await _sig.refresh_ofi(coin)
            _trig_signals = _sig.get_all_signals(coin)
            update_trade_field(trade_id, "ofi_at_trigger", _trig_signals.get("ofi"))
            update_trade_field(trade_id, "funding_rate_at_trigger", _trig_signals.get("funding_rate"))
            update_trade_field(trade_id, "liq_proxy_at_trigger", _trig_signals.get("liq_proxy"))
            update_trade_field(trade_id, "conviction_at_trigger", _trig_signals.get("conviction"))
            update_trade_field(trade_id, "conviction_score_at_trigger", _trig_signals.get("conviction_score"))

            await on_trigger_callback(trade_id, winner, price)
            break

        # ── Early loser sell / re-buy ─────────────────────────────────────────
        now_ts = loop.time()
        trade = get_active_trades().get(trade_id)  # refresh for latest state
        if trade:
            coin = trade.get("coin", "")
            regime_label = trade.get("regime_at_entry") or "NORMAL"
            early_thr, rebuy_thr = _get_early_thresholds(regime_label)
            # BGGDSB: hold both sides to expiry — disable early loser exit
            if trade.get("router_bucket") == "bggdsb":
                early_thr = None
            exit_cfg = CONFIG.get("exit", {})
            cooldown = float(exit_cfg.get("early_loser_cooldown_secs", 90))
            winner_min = float(exit_cfg.get("early_loser_winner_min", 0.57))

            early_sold = trade.get("early_loser_side")
            early_rebought = trade.get("early_loser_rebought")

            if (early_thr is not None
                    and early_sold is None
                    and trade_id not in _early_sell_in_progress
                    and (now_ts - monitor_start) >= cooldown):
                # Check if one side has clearly diverged below threshold
                yes_mid = ws_client.get_mid_price(yes_token)
                no_mid = ws_client.get_mid_price(no_token)
                if yes_mid and no_mid:
                    presume_loser = None
                    if yes_mid < early_thr and no_mid >= winner_min:
                        presume_loser = "YES"
                    elif no_mid < early_thr and yes_mid >= winner_min:
                        presume_loser = "NO"

                    # Duration tracking: record when loser first fell below threshold.
                    # Reset immediately if it recovers above threshold.
                    if presume_loser:
                        if trade_id not in _loser_below_ts:
                            _loser_below_ts[trade_id] = now_ts
                    else:
                        _loser_below_ts.pop(trade_id, None)

                    # Gate 1 — Duration: loser must be below threshold continuously for
                    # early_loser_min_secs_below seconds before we sell. Eliminates
                    # temporary dips that recover within a few seconds.
                    min_secs_below = float(exit_cfg.get("early_loser_min_secs_below", 0))
                    if presume_loser and min_secs_below > 0:
                        secs_below = now_ts - _loser_below_ts.get(trade_id, now_ts)
                        if secs_below < min_secs_below:
                            log.debug("early_loser_duration_gate",
                                      trade_id=trade_id, side=presume_loser,
                                      secs_below=round(secs_below, 1), required=min_secs_below)
                            presume_loser = None

                    # Gate 2 — Velocity: only sell if loser price is still falling.
                    # If price has stabilized or started recovering, skip — reversal risk.
                    if presume_loser and exit_cfg.get("early_loser_velocity_gate", False):
                        from . import volatility as _vol_el
                        loser_tok_tmp = yes_token if presume_loser == "YES" else no_token
                        loser_vel = _vol_el.get_price_velocity(loser_tok_tmp)
                        if loser_vel is not None and loser_vel > 0:
                            log.info("early_loser_velocity_gate_blocked",
                                     trade_id=trade_id, side=presume_loser,
                                     velocity=round(loser_vel, 4))
                            presume_loser = None

                    # Gate 3 — Conviction: if our Binance OFI signals say this side
                    # should WIN, do not sell it early. The market and signals disagree;
                    # trust signals over a temporary price dip on the CLOB.
                    if presume_loser:
                        from . import signals as _sig_el
                        conv_dir, _ = _sig_el.get_conviction(coin)
                        if (conv_dir == "UP" and presume_loser == "YES") or \
                                (conv_dir == "DOWN" and presume_loser == "NO"):
                            log.info("early_loser_conviction_gate",
                                     trade_id=trade_id, side=presume_loser,
                                     conviction=conv_dir)
                            presume_loser = None

                    if presume_loser:
                        _early_sell_in_progress.add(trade_id)
                        base_sz = float(trade.get("entry_size") or 2.0)
                        loser_sz = (float(trade.get("yes_size") or base_sz)
                                    if presume_loser == "YES"
                                    else float(trade.get("no_size") or base_sz))
                        loser_tok = yes_token if presume_loser == "YES" else no_token
                        trade_mode = trade.get("mode", "paper_hybrid")
                        is_paper = trade_mode.startswith("paper")
                        asyncio.create_task(_sell_early_loser_task(
                            trade_id, presume_loser, loser_tok, loser_sz, is_paper, coin
                        ))
                        log.info("early_loser_sell_triggered",
                                 trade_id=trade_id, coin=coin, side=presume_loser,
                                 yes_mid=round(yes_mid, 4), no_mid=round(no_mid, 4),
                                 threshold=early_thr, regime=regime_label)

            elif (rebuy_thr is not None
                    and early_sold is not None
                    and not early_rebought
                    and trade_id not in _rebuy_in_progress):
                # Check if the early-sold side has recovered (wrong-side safeguard)
                sold_tok = yes_token if early_sold == "YES" else no_token
                sold_mid = ws_client.get_mid_price(sold_tok)
                if sold_mid and sold_mid >= rebuy_thr:
                    _rebuy_in_progress.add(trade_id)
                    base_sz = float(trade.get("entry_size") or 2.0)
                    sold_sz = (float(trade.get("yes_size") or base_sz)
                               if early_sold == "YES"
                               else float(trade.get("no_size") or base_sz))
                    trade_mode = trade.get("mode", "paper_hybrid")
                    is_paper = trade_mode.startswith("paper")
                    asyncio.create_task(_rebuy_early_sold_task(
                        trade_id, early_sold, sold_tok, sold_sz, is_paper, coin
                    ))
                    log.info("early_loser_rebuy_triggered",
                             trade_id=trade_id, coin=coin, side=early_sold,
                             sold_mid=round(sold_mid, 4), rebuy_thr=rebuy_thr)

        # ── Save it: hedge reversal by buying the new leading side ─────────────
        if (trade
                and trade.get("save_it_executed") != 1
                and trade_id not in _save_it_in_progress
                and CONFIG.get("router", {}).get("save_it_enabled", True)):
            _si = _save_it_state.setdefault(trade_id, {"peak_yes": 0.0, "peak_no": 0.0})
            y_mid = ws_client.get_mid_price(yes_token) if yes_token else None
            n_mid = ws_client.get_mid_price(no_token) if no_token else None
            if y_mid is not None and n_mid is not None:
                if y_mid > _si["peak_yes"]:
                    _si["peak_yes"] = y_mid
                if n_mid > _si["peak_no"]:
                    _si["peak_no"] = n_mid
                # Time remaining check
                if window_end_dt:
                    secs_left = (window_end_dt - datetime.now(timezone.utc)).total_seconds()
                else:
                    secs_left = 999.0
                # YES reversed: YES dropped ≥0.20 from peak, NO is now leading
                if (secs_left >= 120
                        and _si["peak_yes"] - y_mid >= 0.20
                        and n_mid >= 0.52):
                    vel_no = None
                    from . import volatility as _vol
                    if no_token:
                        vel_no = _vol.get_price_velocity(no_token) or 0.0
                    if vel_no is None or vel_no >= 0:
                        save_eur = float(CONFIG.get("router", {}).get("save_it_size_eur", 3.0))
                        save_shares = round(save_eur / max(n_mid, 0.01), 2)
                        trade_mode = trade.get("mode", "paper_hybrid")
                        is_paper = trade_mode.startswith("paper")
                        _save_it_in_progress.add(trade_id)
                        log.info("save_it_triggered", trade_id=trade_id, coin=coin,
                                 side="NO", y_mid=round(y_mid, 4), n_mid=round(n_mid, 4),
                                 peak_yes=round(_si["peak_yes"], 4), secs_left=round(secs_left, 1))
                        asyncio.create_task(_save_it_task(
                            trade_id, "NO", no_token, save_shares, is_paper, coin
                        ))
                # NO reversed: NO dropped ≥0.20 from peak, YES is now leading
                elif (secs_left >= 120
                        and _si["peak_no"] - n_mid >= 0.20
                        and y_mid >= 0.52):
                    vel_yes = None
                    from . import volatility as _vol
                    if yes_token:
                        vel_yes = _vol.get_price_velocity(yes_token) or 0.0
                    if vel_yes is None or vel_yes >= 0:
                        save_eur = float(CONFIG.get("router", {}).get("save_it_size_eur", 3.0))
                        save_shares = round(save_eur / max(y_mid, 0.01), 2)
                        trade_mode = trade.get("mode", "paper_hybrid")
                        is_paper = trade_mode.startswith("paper")
                        _save_it_in_progress.add(trade_id)
                        log.info("save_it_triggered", trade_id=trade_id, coin=coin,
                                 side="YES", y_mid=round(y_mid, 4), n_mid=round(n_mid, 4),
                                 peak_no=round(_si["peak_no"], 4), secs_left=round(secs_left, 1))
                        asyncio.create_task(_save_it_task(
                            trade_id, "YES", yes_token, save_shares, is_paper, coin
                        ))

        # ── BGGDSB Hedge watcher: koop tegenovergestelde kant zodra ≤ trigger prijs ──
        if (trade
                and trade.get("router_bucket") == "bggdsb"
                and not trade.get("bggdsb_hedge_placed")
                and trade_id not in _bggdsb_hedge_in_progress):
            dom = trade.get("bggdsb_dominant_side")
            if dom:
                hedge_tok = no_token if dom == "YES" else yes_token
                if hedge_tok:
                    hedge_mid = ws_client.get_mid_price(hedge_tok)
                    hedge_trigger = float(CONFIG.get("bggdsb", {}).get("hedge_price_trigger", 0.15))
                    if hedge_mid is not None and 0 < hedge_mid <= hedge_trigger:
                        from .db_sync import get_state as _get_state
                        budget_eur = float(_get_state("bggdsb_window_budget") or
                                           CONFIG.get("bggdsb", {}).get("window_budget_eur", 30.0))
                        hedge_eur = budget_eur * 0.11
                        hedge_shares = round(hedge_eur / max(hedge_mid, 0.01), 2)
                        is_paper = trade.get("mode", "paper").startswith("paper")
                        _bggdsb_hedge_in_progress.add(trade_id)
                        log.info("bggdsb_hedge_triggered", trade_id=trade_id, coin=coin,
                                 dominant_side=dom, hedge_mid=round(hedge_mid, 4),
                                 hedge_shares=hedge_shares)
                        asyncio.create_task(_bggdsb_hedge_task(
                            trade_id, dom, hedge_tok, hedge_shares, is_paper, coin
                        ))

        # Periodic snapshot
        if now_ts - last_snapshot >= snapshot_interval:
            yes_mid = ws_client.get_mid_price(yes_token) if yes_token else None
            no_mid = ws_client.get_mid_price(no_token) if no_token else None
            if yes_mid is not None:
                await write_snapshot(trade_id, yes_token or "", "YES",
                                     ws_client.get_best_bid(yes_token) or 0,
                                     ws_client.get_best_ask(yes_token) or 0,
                                     {"yes_mid": yes_mid, "no_mid": no_mid})
            last_snapshot = now_ts

        await asyncio.sleep(0.5)
