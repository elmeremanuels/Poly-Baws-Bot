"""Strategy execution: entry, trigger action, exit logic."""
import asyncio
from datetime import datetime, timezone, timedelta

from . import fill_tracker, orders, paper_trader, volatility as _vol, ws_client
from .config_loader import CONFIG
from .logger import log, write_event, update_trade
from .state import (
    get_active_trades, update_trade_field, remove_active_trade,
    persist_trade, is_paper_mode, get_mode,
)
from .monitor import start_monitoring, stop_monitoring

TRADING_CFG = CONFIG["trading"]
ENTRY_PRICE = TRADING_CFG["entry_price_target"]
MAX_DEV = TRADING_CFG["entry_price_max_deviation"]
CUTOFF_MIN = TRADING_CFG["entry_cutoff_minutes_before_window"]


def _get_trigger_threshold(coin: str) -> float:
    """Per-coin trigger threshold, falling back to global trading.trigger_threshold."""
    return CONFIG["coins"].get(coin, {}).get("trigger_threshold", TRADING_CFG["trigger_threshold"])


async def execute_entry(trade_id: str, broadcast_fn=None) -> bool:
    """
    Place YES and NO limit buy orders. Wait for fills.
    Returns True if both sides filled; handles edge cases.
    """
    trades = get_active_trades()
    trade = trades.get(trade_id)
    if not trade:
        return False

    coin = trade["coin"]
    yes_token = trade["condition_id_yes"]
    no_token = trade["condition_id_no"]
    trade_size_eur = CONFIG["trading"].get("trade_size_eur", 1.0)
    size = round(trade_size_eur / ENTRY_PRICE, 2)
    update_trade_field(trade_id, "entry_size", size)
    # Use the mode stored in the trade (set at creation time) so that mode changes
    # during an active trade don't switch it between paper/live mid-flight.
    trade_mode = trade.get("mode") or get_mode()
    if trade_mode == "live_learning":
        from . import learning as _learning
        paper = _learning.get_orchestrator().get_trading_mode().startswith("paper")
    else:
        paper = trade_mode.startswith("paper")

    window_start = datetime.fromisoformat(trade["window_start_ts"]).astimezone(timezone.utc)
    cutoff_time = window_start - timedelta(minutes=CUTOFF_MIN)

    now_utc = datetime.now(timezone.utc)
    if now_utc >= cutoff_time:
        update_trade_field(trade_id, "status", "aborted")
        update_trade_field(trade_id, "notes", "past_cutoff")
        await persist_trade(trade_id)
        remove_active_trade(trade_id)
        log.info("entry_aborted_past_cutoff", trade_id=trade_id,
                 seconds_past=round((now_utc - cutoff_time).total_seconds(), 1))
        return False

    log.info("entry_starting", trade_id=trade_id, coin=coin, paper=paper)
    await write_event(trade_id, "entry_start", coin, {"paper": paper, "size": size})

    update_trade_field(trade_id, "entry_placed_ts", datetime.now(timezone.utc).isoformat())
    update_trade_field(trade_id, "status", "entry_placed")

    if paper:
        # Simulate limit buy at current best ask — maker order, 0% taker fee.
        # _should_enter already verified spreads are acceptable.
        yes_ask = ws_client.get_best_ask(yes_token) or ENTRY_PRICE
        no_ask = ws_client.get_best_ask(no_token) or ENTRY_PRICE
        yes_result, no_result = await asyncio.gather(
            paper_trader.simulate_limit_buy(yes_token, yes_ask, size),
            paper_trader.simulate_limit_buy(no_token, no_ask, size),
        )
        if not yes_result["filled"] or not no_result["filled"]:
            log.warning("entry_limit_fill_failed", trade_id=trade_id,
                        yes_filled=yes_result["filled"], no_filled=no_result["filled"],
                        yes_ask=yes_ask, no_ask=no_ask)
    else:
        # Live: place at current best ask so the order crosses immediately (taker fill).
        # _should_enter already validated combined ask ≤ max_combined_cost and spread ≤ max_token_spread.
        yes_ask = round(ws_client.get_best_ask(yes_token) or ENTRY_PRICE, 2)
        no_ask = round(ws_client.get_best_ask(no_token) or ENTRY_PRICE, 2)
        yes_resp, no_resp = await asyncio.gather(
            orders.place_limit_order(yes_token, "BUY", yes_ask, size),
            orders.place_limit_order(no_token, "BUY", no_ask, size),
        )
        if yes_resp:
            update_trade_field(trade_id, "yes_order_id", yes_resp["order_id"])
        if no_resp:
            update_trade_field(trade_id, "no_order_id", no_resp["order_id"])

        # Poll for fills until cutoff
        yes_result = await _poll_live_fill(yes_resp["order_id"] if yes_resp else None, cutoff_time)
        no_result = await _poll_live_fill(no_resp["order_id"] if no_resp else None, cutoff_time)

    yes_filled = yes_result.get("filled", False)
    no_filled = no_result.get("filled", False)

    await _handle_fill_results(trade_id, yes_filled, no_filled, yes_result, no_result, paper, broadcast_fn)
    return yes_filled and no_filled


async def _poll_live_fill(order_id: str | None, cutoff: datetime, poll_interval: float = 2.0) -> dict:
    if not order_id:
        return {"filled": False}
    while datetime.now(timezone.utc) < cutoff:
        order = await orders.get_order(order_id)
        if order:
            status = order.get("status")
            if status in ("MATCHED", "FILLED"):
                size_matched = float(order.get("size_matched") or order.get("sizeFilled") or 0)
                avg_price = float(order.get("average_price") or order.get("price") or 0)
                return {"filled": True, "fill_price": avg_price, "filled_size": size_matched, "fees": 0.0}
            if status in ("CANCELED", "UNMATCHED"):
                log.warning("poll_order_cancelled", order_id=order_id, status=status)
                return {"filled": False, "cancelled": True}
        await asyncio.sleep(poll_interval)
    return {"filled": False, "timed_out": True}


async def _handle_fill_results(
    trade_id: str,
    yes_filled: bool,
    no_filled: bool,
    yes_result: dict,
    no_result: dict,
    paper: bool,
    broadcast_fn,
) -> None:
    trade = get_active_trades().get(trade_id)
    if not trade:
        return

    if yes_filled and no_filled:
        update_trade_field(trade_id, "entry_filled_ts", datetime.now(timezone.utc).isoformat())
        update_trade_field(trade_id, "entry_yes_price", yes_result.get("fill_price"))
        update_trade_field(trade_id, "entry_no_price", no_result.get("fill_price"))
        update_trade_field(trade_id, "status", "monitoring")
        fees = (yes_result.get("fees") or 0) + (no_result.get("fees") or 0)
        update_trade_field(trade_id, "fees_paid", fees)
        await write_event(trade_id, "entry_filled", trade["coin"], {
            "yes_price": yes_result.get("fill_price"),
            "no_price": no_result.get("fill_price"),
        })
        await persist_trade(trade_id)
        log.info("entry_filled", trade_id=trade_id)
        if broadcast_fn:
            await broadcast_fn({"event": "entry_filled", "trade_id": trade_id})
        await start_monitoring(trade_id, lambda tid, w, p: on_trigger(tid, w, p, broadcast_fn))

    elif yes_filled and not no_filled:
        await _abort_partial(trade_id, "YES", yes_result, paper)
    elif no_filled and not yes_filled:
        await _abort_partial(trade_id, "NO", no_result, paper)
    else:
        # Cancel any resting orders on Polymarket before abandoning
        if not paper:
            for oid_field in ("yes_order_id", "no_order_id"):
                oid = trade.get(oid_field)
                if oid:
                    await orders.cancel_order(oid)
        update_trade_field(trade_id, "status", "aborted")
        update_trade_field(trade_id, "notes", "no_fills_at_cutoff")
        await persist_trade(trade_id)
        remove_active_trade(trade_id)
        await write_event(trade_id, "abort_no_fills", trade["coin"], {})
        log.info("entry_aborted_no_fills", trade_id=trade_id)


async def _abort_partial(trade_id: str, filled_side: str, fill_result: dict, paper: bool) -> None:
    trade = get_active_trades().get(trade_id)
    if not trade:
        return

    log.warning("partial_fill_abort", trade_id=trade_id, filled_side=filled_side)
    await write_event(trade_id, "partial_fill_abort", trade["coin"], {"filled_side": filled_side})

    if not paper:
        # Cancel unfilled leg
        other_order_id = trade.get("no_order_id" if filled_side == "YES" else "yes_order_id")
        if other_order_id:
            await orders.cancel_order(other_order_id)

        # Market sell the filled leg
        token_id = trade["condition_id_yes"] if filled_side == "YES" else trade["condition_id_no"]
        size = fill_result.get("filled_size") or trade["entry_size"]
        await orders.place_market_order(token_id, "SELL", size)
    else:
        # Paper: just simulate market sell
        token_id = trade["condition_id_yes"] if filled_side == "YES" else trade["condition_id_no"]
        size = fill_result.get("filled_size") or trade["entry_size"]
        await paper_trader.simulate_market_sell(token_id, size)

    update_trade_field(trade_id, "status", "aborted")
    update_trade_field(trade_id, "notes", f"partial_fill_{filled_side.lower()}_only")
    await persist_trade(trade_id)
    remove_active_trade(trade_id)


async def on_trigger(trade_id: str, winner: str, price: float | None, broadcast_fn=None) -> None:
    """Called when trigger condition fires or window resolves."""
    await stop_monitoring(trade_id)

    if winner == "RESOLUTION":
        await _handle_resolution(trade_id, broadcast_fn)
        return

    trade = get_active_trades().get(trade_id)
    if not trade:
        return

    # Use the mode stored at trade creation time so a mode change mid-trade
    # doesn't switch between paper/live execution.
    trade_mode = trade.get("mode") or get_mode()
    if trade_mode == "live_learning":
        from . import learning as _learning
        paper = _learning.get_orchestrator().get_trading_mode().startswith("paper")
    else:
        paper = trade_mode.startswith("paper")
    coin = trade["coin"]
    yes_token = trade["condition_id_yes"]
    no_token = trade["condition_id_no"]
    size = trade["entry_size"]

    loser_side = "NO" if winner == "YES" else "YES"
    loser_token = no_token if loser_side == "NO" else yes_token
    winner_token = yes_token if winner == "YES" else no_token

    log.info("executing_trigger_action", trade_id=trade_id, winner=winner, loser=loser_side)

    # Step 1: market sell loser
    if paper:
        loser_result = await paper_trader.simulate_market_sell(loser_token, size)
    else:
        loser_resp = await orders.place_market_order(loser_token, "SELL", size)
        loser_result = {"filled": bool(loser_resp), "fill_price": 0.30, "fees": 0.0}

    loser_price = loser_result.get("fill_price") or 0.30
    loser_fees = loser_result.get("fees") or 0.0
    update_trade_field(trade_id, "loser_exit_price", loser_price)
    update_trade_field(trade_id, "loser_exit_ts", datetime.now(timezone.utc).isoformat())

    await write_event(trade_id, "loser_sold", coin, {"side": loser_side, "price": loser_price})

    current_fees = trade.get("fees_paid") or 0.0
    total_fees_so_far = current_fees + loser_fees
    update_trade_field(trade_id, "fees_paid", total_fees_so_far)

    # Compute break-even winner price: the minimum winner sell that recovers total cost
    entry_yes = trade.get("entry_yes_price") or ENTRY_PRICE
    entry_no = trade.get("entry_no_price") or ENTRY_PRICE
    total_cost = (entry_yes + entry_no) * size + total_fees_so_far
    loser_proceeds = loser_price * size
    be_price = max(0.0, min(1.0, round((total_cost - loser_proceeds) / size, 4)))
    update_trade_field(trade_id, "break_even_price", be_price)
    update_trade_field(trade_id, "actual_winner", winner)
    await write_event(trade_id, "break_even_computed", coin, {"break_even_price": be_price})

    # Step 2: single resting target order + stop-market via price monitoring
    asyncio.create_task(_winner_exit_oco(trade_id, winner_token, size, paper, broadcast_fn))
    update_trade_field(trade_id, "status", "exiting")
    await persist_trade(trade_id)
    if broadcast_fn:
        await broadcast_fn({"event": "trigger_hit", "trade_id": trade_id, "winner": winner})


async def _winner_exit_oco(
    trade_id: str,
    winner_token: str,
    size: float,
    paper: bool,
    broadcast_fn,
) -> None:
    """
    Single resting limit sell @ TARGET_EXIT (80¢) + stop-market trigger at STOP_EXIT (60¢).

    Only ONE order rests in the book at a time to avoid share-locking issues and short-position
    risk. The 60¢ stop is implemented as a price trigger → cancel limit → market sell, not as a
    second resting limit order. This guarantees exit even on gappy price moves (stop-market
    semantics), at the cost of possible sub-60¢ fill on fast drops.
    """
    trade = get_active_trades().get(trade_id)
    if not trade:
        return

    window_end = trade.get("window_end_ts")
    window_end_dt = datetime.fromisoformat(window_end).astimezone(timezone.utc) if window_end else None

    if paper:
        await _winner_exit_paper(trade_id, winner_token, size, window_end_dt, broadcast_fn)
    else:
        await _winner_exit_live(trade_id, winner_token, size, window_end_dt, broadcast_fn)


def _store_trail_metrics(trade_id: str, peak_bid: float, ratchet_count: int, time_in_trail: float) -> None:
    update_trade_field(trade_id, "peak_bid", round(peak_bid, 4))
    update_trade_field(trade_id, "ratchet_count", ratchet_count)
    update_trade_field(trade_id, "time_in_trail_seconds", round(time_in_trail, 2))


def _add_winner_fees(trade_id: str, fees: float) -> None:
    current = (get_active_trades().get(trade_id) or {}).get("fees_paid") or 0.0
    update_trade_field(trade_id, "fees_paid", round(current + fees, 6))


# ── Peg-Cross Exit Engine ─────────────────────────────────────────────────────

def crossing_cost(mid: float, spread: float, size: float) -> float:
    """Cost (€) of converting a resting limit sell to a market sell.
    Includes taker fee + selling at bid instead of mid."""
    bid = max(0.01, mid - spread / 2)
    taker = bid * paper_trader.taker_fee_rate(bid) * size
    spread_loss = (spread / 2) * size
    return taker + spread_loss


def compute_cross_score(
    mid: float,
    peak_mid: float,
    current_limit: float,
    best_bid: float | None,
    best_ask: float | None,
    seconds_left: float,
    cfg: dict,
    size: float = 2.0,
    velocity: float = 0.0,
) -> tuple[float, str]:
    """
    Returns (score 0–1, dominant_reason). score ≥ cross_threshold → market sell.

    Components:
      0.30 time_urgency         — rises as window end approaches
      0.30 fee_adjusted         — expected peg loss vs €0.11 crossing cost
      0.25 mid_decay            — mid dropped from peak → momentum reversed
      0.15 fill_unlikely        — limit above best ask → passive fill impossible
    """
    urgency_window = cfg.get("time_urgency_window", 120)
    time_urgency = max(0.0, min(1.0, 1.0 - seconds_left / urgency_window))

    spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else 0.06

    if velocity < 0:
        cost = crossing_cost(mid, spread, size)
        expected_peg_loss = abs(velocity) * min(seconds_left, 60) * size
        fee_adjusted = min(1.0, min(2.0, expected_peg_loss / max(0.001, cost)) / 2.0)
    else:
        fee_adjusted = 0.0

    decay = max(0.0, peak_mid - mid)
    mid_decay = min(1.0, decay / 0.03)

    if best_ask is not None:
        fill_unlikely = 1.0 if current_limit > best_ask + 0.005 else 0.0
    else:
        fill_unlikely = 1.0

    score = round(
        0.30 * time_urgency
        + 0.30 * fee_adjusted
        + 0.25 * mid_decay
        + 0.15 * fill_unlikely,
        4,
    )
    components = {
        "time_urgency": time_urgency,
        "fee_loss_vs_crossing_cost": fee_adjusted,
        "mid_decay": mid_decay,
        "fill_unlikely": fill_unlikely,
    }
    reason = max(components, key=components.get)
    return score, reason


def get_exit_phase(seconds_left: float) -> str:
    if seconds_left > 90:
        return "patient"
    elif seconds_left > 30:
        return "urgent"
    return "force"


def get_phase_params(phase: str, cfg: dict) -> dict:
    base_threshold = cfg["cross_threshold"]
    base_interval = cfg["check_interval_seconds"]
    base_buffer = cfg["ratchet_buffer"]
    if phase == "patient":
        return {"cross_threshold": base_threshold, "check_interval": base_interval, "ratchet_buffer": base_buffer}
    elif phase == "urgent":
        return {"cross_threshold": 0.50, "check_interval": max(0.5, base_interval / 2), "ratchet_buffer": base_buffer}
    return {"cross_threshold": 0.0, "check_interval": 0.2, "ratchet_buffer": 0.0}


async def _winner_exit_paper(
    trade_id: str,
    winner_token: str,
    size: float,
    window_end_dt: datetime | None,
    broadcast_fn,
) -> None:
    """Paper mode: peg-cross exit engine — resting limit + dynamic market conversion."""
    trade = get_active_trades().get(trade_id)
    coin = trade["coin"] if trade else "UNKNOWN"
    es = _vol.get_coin_params(coin)
    trigger_price = _get_trigger_threshold(coin)
    break_even_price = trade.get("break_even_price") if trade else None
    # Initial limit is at least break_even + 1¢ so we never rest below profitability
    raw_limit = round(trigger_price + es["initial_offset"], 2)
    if break_even_price is not None and break_even_price > 0:
        raw_limit = max(raw_limit, round(break_even_price + es.get("min_winner_profit_margin", 0.03), 2))
    current_limit = raw_limit
    peak_mid = trigger_price
    ratchet_count = 0
    trail_start = asyncio.get_event_loop().time()

    await write_event(trade_id, "trailing_started", coin, {
        "initial_limit": current_limit, "trigger_price": trigger_price,
        "break_even_price": break_even_price,
    })

    while True:
        now_dt = datetime.now(timezone.utc)
        loop_time = asyncio.get_event_loop().time()
        seconds_left = (window_end_dt - now_dt).total_seconds() if window_end_dt else 300.0

        if seconds_left <= es["force_exit_seconds"]:
            hold_threshold = es.get("hold_for_resolution_mid_threshold", 0.90)
            if break_even_price and break_even_price > 0:
                hold_threshold = min(hold_threshold, break_even_price)
            mid_check = ws_client.get_mid_price(winner_token)
            if mid_check is not None and mid_check >= hold_threshold:
                _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
                await write_event(trade_id, "held_for_resolution", coin, {
                    "mid": mid_check, "seconds_left": round(seconds_left, 1),
                })
                await _close_trade(trade_id, mid_check, "held_for_resolution", broadcast_fn)
                return
            result = await paper_trader.simulate_market_sell(winner_token, size)
            _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
            await fill_tracker.record_fill_attempt(trade_id, winner_token, "sell", current_limit, False)
            _add_winner_fees(trade_id, result.get("fees", 0.0))
            await _close_trade(trade_id, result.get("fill_price"), "force_exit_window_end", broadcast_fn)
            return

        phase = get_exit_phase(seconds_left)
        params = get_phase_params(phase, es)

        target_result = await paper_trader.simulate_limit_sell(winner_token, current_limit, size)
        if target_result["filled"]:
            _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
            await fill_tracker.record_fill_attempt(trade_id, winner_token, "sell", current_limit, True, target_result["fill_price"])
            _add_winner_fees(trade_id, target_result.get("fees", 0.0))
            await _close_trade(trade_id, target_result["fill_price"], "limit_filled", broadcast_fn)
            return

        mid = ws_client.get_mid_price(winner_token)
        best_bid = ws_client.get_best_bid(winner_token)
        best_ask = ws_client.get_best_ask(winner_token)

        if mid is not None:
            if mid > peak_mid:
                peak_mid = mid

            vel = _vol.get_price_velocity(winner_token)
            score, reason = compute_cross_score(
                mid, peak_mid, current_limit, best_bid, best_ask, seconds_left, es,
                size=size, velocity=vel,
            )

            if score >= params["cross_threshold"]:
                # Don't market-sell a profitable position in patient/urgent phase;
                # let the resting limit fill or force_exit handle it.
                if break_even_price and mid > break_even_price and phase != "force":
                    pass
                else:
                    result = await paper_trader.simulate_market_sell(winner_token, size)
                    _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
                    await fill_tracker.record_fill_attempt(trade_id, winner_token, "sell", current_limit, False)
                    _add_winner_fees(trade_id, result.get("fees", 0.0))
                    await write_event(trade_id, "peg_cross_triggered", coin, {
                        "score": score, "reason": reason, "phase": phase,
                        "mid": mid, "velocity": round(vel, 5), "seconds_left": round(seconds_left, 1),
                    })
                    await _close_trade(trade_id, result.get("fill_price"), "peg_cross", broadcast_fn)
                    return

            if mid > current_limit:
                new_limit = round(mid + params["ratchet_buffer"], 2)
                if new_limit > current_limit:
                    await write_event(trade_id, "limit_ratcheted", coin, {
                        "from": current_limit, "to": new_limit, "mid": mid,
                    })
                    current_limit = new_limit
                    ratchet_count += 1

        await asyncio.sleep(params["check_interval"])


async def _winner_exit_live(
    trade_id: str,
    winner_token: str,
    size: float,
    window_end_dt: datetime | None,
    broadcast_fn,
) -> None:
    """Live mode: peg-cross exit engine — real limit order + dynamic market conversion."""
    trade = get_active_trades().get(trade_id)
    coin = trade["coin"] if trade else "UNKNOWN"
    es = _vol.get_coin_params(coin)
    trigger_price = _get_trigger_threshold(coin)
    break_even_price = trade.get("break_even_price") if trade else None
    raw_limit = round(trigger_price + es["initial_offset"], 2)
    if break_even_price is not None and break_even_price > 0:
        raw_limit = max(raw_limit, round(break_even_price + es.get("min_winner_profit_margin", 0.03), 2))
    current_limit = raw_limit
    peak_mid = trigger_price
    ratchet_count = 0
    trail_start = asyncio.get_event_loop().time()
    last_status_check = trail_start

    limit_resp = await orders.place_limit_order(winner_token, "SELL", current_limit, size)
    current_order_id = limit_resp["order_id"] if limit_resp else None
    if not current_order_id:
        log.error("winner_initial_limit_failed", trade_id=trade_id)
        await orders.place_market_order(winner_token, "SELL", size)
        _store_trail_metrics(trade_id, peak_mid, 0, 0)
        await _close_trade(trade_id, None, "peg_cross", broadcast_fn)
        return

    await write_event(trade_id, "trailing_started", coin, {
        "initial_limit": current_limit, "trigger_price": trigger_price,
    })

    while True:
        now_dt = datetime.now(timezone.utc)
        loop_time = asyncio.get_event_loop().time()
        seconds_left = (window_end_dt - now_dt).total_seconds() if window_end_dt else 300.0

        if seconds_left <= es["force_exit_seconds"]:
            hold_threshold = es.get("hold_for_resolution_mid_threshold", 0.90)
            if break_even_price and break_even_price > 0:
                hold_threshold = min(hold_threshold, break_even_price)
            mid_check = ws_client.get_mid_price(winner_token)
            await orders.cancel_order(current_order_id)
            if mid_check is not None and mid_check >= hold_threshold:
                _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
                await write_event(trade_id, "held_for_resolution", coin, {
                    "mid": mid_check, "seconds_left": round(seconds_left, 1),
                    "note": "winner_tokens_need_redemption_in_polymarket_wallet",
                })
                await _close_trade(trade_id, mid_check, "held_for_resolution", broadcast_fn)
                return
            await orders.place_market_order(winner_token, "SELL", size)
            _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
            await fill_tracker.record_fill_attempt(trade_id, winner_token, "sell", current_limit, False)
            _add_winner_fees(trade_id, paper_trader.taker_fee_rate(mid_check or 0.5) * size)
            await _close_trade(trade_id, mid_check, "force_exit_window_end", broadcast_fn)
            return

        phase = get_exit_phase(seconds_left)
        params = get_phase_params(phase, es)

        mid = ws_client.get_mid_price(winner_token)
        best_bid = ws_client.get_best_bid(winner_token)
        best_ask = ws_client.get_best_ask(winner_token)

        if mid is not None:
            if mid > peak_mid:
                peak_mid = mid

            vel = _vol.get_price_velocity(winner_token)
            score, reason = compute_cross_score(
                mid, peak_mid, current_limit, best_bid, best_ask, seconds_left, es,
                size=size, velocity=vel,
            )

            if score >= params["cross_threshold"]:
                if break_even_price and mid > break_even_price and phase != "force":
                    pass
                else:
                    await orders.cancel_order(current_order_id)
                    await orders.place_market_order(winner_token, "SELL", size)
                    _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
                    await fill_tracker.record_fill_attempt(trade_id, winner_token, "sell", current_limit, False)
                    _add_winner_fees(trade_id, paper_trader.taker_fee_rate(mid or 0.5) * size)
                    await write_event(trade_id, "peg_cross_triggered", coin, {
                        "score": score, "reason": reason, "phase": phase,
                        "mid": mid, "velocity": round(vel, 5), "seconds_left": round(seconds_left, 1),
                    })
                    await _close_trade(trade_id, mid, "peg_cross", broadcast_fn)
                    return

            if mid > current_limit:
                new_limit = round(mid + params["ratchet_buffer"], 2)
                if new_limit > current_limit:
                    cancelled = await orders.cancel_order(current_order_id)
                    if cancelled:
                        new_resp = await orders.place_limit_order(winner_token, "SELL", new_limit, size)
                        if new_resp and new_resp.get("order_id"):
                            current_order_id = new_resp["order_id"]
                            await write_event(trade_id, "limit_ratcheted", coin, {
                                "from": current_limit, "to": new_limit, "mid": mid,
                            })
                            current_limit = new_limit
                            ratchet_count += 1
                        else:
                            log.warning("ratchet_reissue_failed_market_exit", trade_id=trade_id)
                            await orders.place_market_order(winner_token, "SELL", size)
                            _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
                            _add_winner_fees(trade_id, paper_trader.taker_fee_rate(mid or 0.5) * size)
                            await _close_trade(trade_id, mid, "peg_cross", broadcast_fn)
                            return
                    else:
                        order = await orders.get_order(current_order_id)
                        if order and order.get("status") in ("MATCHED", "FILLED"):
                            fill = float(order.get("average_price") or current_limit)
                            _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
                            await fill_tracker.record_fill_attempt(trade_id, winner_token, "sell", current_limit, True, fill)
                            _add_winner_fees(trade_id, paper_trader.SLIPPAGE_BUFFER * size)
                            await _close_trade(trade_id, fill, "limit_filled", broadcast_fn)
                            return

        if loop_time - last_status_check >= 5.0:
            order = await orders.get_order(current_order_id)
            last_status_check = loop_time
            if order and order.get("status") in ("MATCHED", "FILLED"):
                fill = float(order.get("average_price") or current_limit)
                _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
                await fill_tracker.record_fill_attempt(trade_id, winner_token, "sell", current_limit, True, fill)
                _add_winner_fees(trade_id, paper_trader.SLIPPAGE_BUFFER * size)
                await _close_trade(trade_id, fill, "limit_filled", broadcast_fn)
                return

        await asyncio.sleep(params["check_interval"])


async def _close_trade(trade_id: str, fill_price: float | None, reason: str, broadcast_fn) -> None:
    trade = get_active_trades().get(trade_id)
    if not trade:
        return

    now = datetime.now(timezone.utc).isoformat()
    update_trade_field(trade_id, "winner_exit_price", fill_price)
    update_trade_field(trade_id, "winner_exit_ts", now)
    update_trade_field(trade_id, "winner_exit_reason", reason)
    update_trade_field(trade_id, "status", "closed")

    # Calculate P&L
    entry_yes = trade.get("entry_yes_price") or ENTRY_PRICE
    entry_no = trade.get("entry_no_price") or ENTRY_PRICE
    size = trade.get("entry_size") or 2
    loser_price = trade.get("loser_exit_price") or 0.30
    winner_price = fill_price or 1.0  # resolution = $1

    # Cost basis: (entry_yes + entry_no) * size
    cost = (entry_yes + entry_no) * size
    proceeds = (loser_price + (winner_price or 0)) * size
    fees = trade.get("fees_paid") or 0.0
    gross_pnl = proceeds - cost
    net_pnl = gross_pnl - fees

    update_trade_field(trade_id, "gross_pnl", round(gross_pnl, 4))
    update_trade_field(trade_id, "net_pnl", round(net_pnl, 4))

    await persist_trade(trade_id)
    await write_event(trade_id, "trade_closed", trade["coin"], {
        "reason": reason, "net_pnl": net_pnl, "fill_price": fill_price
    })
    remove_active_trade(trade_id)

    log.info("trade_closed", trade_id=trade_id, reason=reason, net_pnl=net_pnl)
    if broadcast_fn:
        await broadcast_fn({"event": "trade_closed", "trade_id": trade_id, "reason": reason, "net_pnl": net_pnl})


async def _handle_resolution(trade_id: str, broadcast_fn) -> None:
    """Window expired — both legs settle on-chain ($1 winner / $0 loser).
    Since the bot holds BOTH sides, total proceeds = 1.0 × size regardless of which
    side wins. P&L = proceeds - cost - fees."""
    trade = get_active_trades().get(trade_id)
    if not trade:
        return

    entry_yes = trade.get("entry_yes_price") or ENTRY_PRICE
    entry_no = trade.get("entry_no_price") or ENTRY_PRICE
    size = trade.get("entry_size") or 2
    fees = trade.get("fees_paid") or 0.0
    gross_pnl = round(1.0 * size - (entry_yes + entry_no) * size, 4)
    net_pnl = round(gross_pnl - fees, 4)

    update_trade_field(trade_id, "winner_exit_reason", "resolution")
    update_trade_field(trade_id, "gross_pnl", gross_pnl)
    update_trade_field(trade_id, "net_pnl", net_pnl)
    update_trade_field(trade_id, "status", "resolved")
    await persist_trade(trade_id)
    remove_active_trade(trade_id)
    await write_event(trade_id, "resolution", trade["coin"], {"net_pnl": net_pnl})
    log.info("trade_resolved", trade_id=trade_id, net_pnl=net_pnl)
    if broadcast_fn:
        await broadcast_fn({"event": "trade_resolved", "trade_id": trade_id})
