"""Strategy execution: entry, trigger action, exit logic."""
import asyncio
from datetime import datetime, timezone, timedelta

from . import orders, paper_trader, ws_client
from .config_loader import CONFIG
from .logger import log, write_event, update_trade
from .state import (
    get_active_trades, update_trade_field, remove_active_trade,
    persist_trade, is_paper_mode,
)
from .monitor import start_monitoring, stop_monitoring

TRADING_CFG = CONFIG["trading"]
ENTRY_PRICE = TRADING_CFG["entry_price_target"]
MAX_DEV = TRADING_CFG["entry_price_max_deviation"]
CUTOFF_MIN = TRADING_CFG["entry_cutoff_minutes_before_window"]
TARGET_EXIT = TRADING_CFG["target_exit"]
STOP_EXIT = TRADING_CFG["stop_exit"]


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
    paper = is_paper_mode()

    window_start = datetime.fromisoformat(trade["window_start_ts"]).astimezone(timezone.utc)
    cutoff_time = window_start - timedelta(minutes=CUTOFF_MIN)

    log.info("entry_starting", trade_id=trade_id, coin=coin, paper=paper)
    await write_event(trade_id, "entry_start", coin, {"paper": paper, "size": size})

    update_trade_field(trade_id, "entry_placed_ts", datetime.now(timezone.utc).isoformat())
    update_trade_field(trade_id, "status", "entry_placed")

    if paper:
        yes_result, no_result = await asyncio.gather(
            paper_trader.poll_for_fill(yes_token, ENTRY_PRICE + MAX_DEV, size, "buy",
                                       timeout_seconds=(cutoff_time - datetime.now(timezone.utc)).total_seconds()),
            paper_trader.poll_for_fill(no_token, ENTRY_PRICE + MAX_DEV, size, "buy",
                                       timeout_seconds=(cutoff_time - datetime.now(timezone.utc)).total_seconds()),
        )
    else:
        # Live: place both simultaneously
        yes_resp, no_resp = await asyncio.gather(
            orders.place_limit_order(yes_token, "BUY", ENTRY_PRICE, size),
            orders.place_limit_order(no_token, "BUY", ENTRY_PRICE, size),
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
        if order and order.get("status") in ("MATCHED", "FILLED"):
            size_matched = float(order.get("size_matched") or order.get("sizeFilled") or 0)
            avg_price = float(order.get("average_price") or order.get("price") or 0)
            return {"filled": True, "fill_price": avg_price, "filled_size": size_matched, "fees": 0.0}
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

    paper = is_paper_mode()
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

    # Step 2: single resting target order + stop-market via price monitoring
    asyncio.create_task(_winner_exit_oco(trade_id, winner_token, size, paper, broadcast_fn))

    current_fees = trade.get("fees_paid") or 0.0
    update_trade_field(trade_id, "fees_paid", current_fees + loser_fees)
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


async def _winner_exit_paper(
    trade_id: str,
    winner_token: str,
    size: float,
    window_end_dt: datetime | None,
    broadcast_fn,
) -> None:
    """Paper mode: simulate single resting 80¢ limit; stop-market if mid ≤ 60¢."""
    while True:
        now = datetime.now(timezone.utc)

        if window_end_dt and now >= window_end_dt:
            await _close_trade(trade_id, None, "resolution", broadcast_fn)
            return

        # Target: check if limit sell @ 80¢ would fill (best bid >= target)
        target_result = await paper_trader.simulate_limit_sell(winner_token, TARGET_EXIT, size)
        if target_result["filled"]:
            await _close_trade(trade_id, target_result["fill_price"], "target_80", broadcast_fn)
            return

        # Stop: if mid price has dropped to ≤ 60¢, execute stop-market sell immediately
        mid = ws_client.get_mid_price(winner_token)
        if mid is not None and mid <= STOP_EXIT:
            stop_result = await paper_trader.simulate_market_sell(winner_token, size)
            fill_price = stop_result.get("fill_price")
            log.info("stop_market_triggered_paper", trade_id=trade_id, mid=mid, fill=fill_price)
            await _close_trade(trade_id, fill_price, "stop_60", broadcast_fn)
            return

        await asyncio.sleep(0.5)


async def _winner_exit_live(
    trade_id: str,
    winner_token: str,
    size: float,
    window_end_dt: datetime | None,
    broadcast_fn,
) -> None:
    """
    Live mode:
    1. Place single resting limit sell @ 80¢.
    2. Poll WS price every 0.5s; if mid ≤ 60¢: cancel limit → market sell (stop-market).
    3. Poll order status via REST every 5s to catch fills the price-check missed.
    4. On window expiry: cancel order → log resolution (Polymarket settles on-chain).
    """
    # Place the single target order
    limit_resp = await orders.place_limit_order(winner_token, "SELL", TARGET_EXIT, size)
    target_order_id = limit_resp["order_id"] if limit_resp else None
    if not target_order_id:
        # Fallback: can't place order → market sell now and log as stop
        log.error("winner_limit_order_failed", trade_id=trade_id)
        resp = await orders.place_market_order(winner_token, "SELL", size)
        await _close_trade(trade_id, None, "stop_60", broadcast_fn)
        return

    log.info("winner_limit_placed", trade_id=trade_id, order_id=target_order_id, price=TARGET_EXIT)

    last_status_check = asyncio.get_event_loop().time()
    STATUS_POLL_INTERVAL = 5.0

    while True:
        now = datetime.now(timezone.utc)
        loop_time = asyncio.get_event_loop().time()

        if window_end_dt and now >= window_end_dt:
            await orders.cancel_order(target_order_id)
            await _close_trade(trade_id, None, "resolution", broadcast_fn)
            return

        # Fast path: WS price check for stop trigger
        mid = ws_client.get_mid_price(winner_token)
        if mid is not None and mid <= STOP_EXIT:
            cancelled = await orders.cancel_order(target_order_id)
            if cancelled:
                # Limit order successfully cancelled; market sell all shares
                log.info("stop_market_triggered_live", trade_id=trade_id, mid=mid)
                resp = await orders.place_market_order(winner_token, "SELL", size)
                actual_price = mid  # Best estimate; real fill may differ
                await _close_trade(trade_id, actual_price, "stop_60", broadcast_fn)
            else:
                # Cancel failed → order may have just filled at target; check status
                order = await orders.get_order(target_order_id)
                if order and order.get("status") in ("MATCHED", "FILLED"):
                    fill = float(order.get("average_price") or TARGET_EXIT)
                    await _close_trade(trade_id, fill, "target_80", broadcast_fn)
                else:
                    # Unknown state: force market sell to guarantee exit
                    log.warning("cancel_failed_forcing_market_sell", trade_id=trade_id)
                    await orders.place_market_order(winner_token, "SELL", size)
                    await _close_trade(trade_id, mid, "stop_60", broadcast_fn)
            return

        # Periodic REST poll to catch target fills
        if loop_time - last_status_check >= STATUS_POLL_INTERVAL:
            order = await orders.get_order(target_order_id)
            last_status_check = loop_time
            if order and order.get("status") in ("MATCHED", "FILLED"):
                fill = float(order.get("average_price") or TARGET_EXIT)
                log.info("target_order_filled", trade_id=trade_id, fill=fill)
                await _close_trade(trade_id, fill, "target_80", broadcast_fn)
                return

        await asyncio.sleep(0.5)


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
    """Window expired — position resolves on-chain."""
    trade = get_active_trades().get(trade_id)
    if not trade:
        return
    update_trade_field(trade_id, "winner_exit_reason", "resolution")
    update_trade_field(trade_id, "status", "resolved")
    await persist_trade(trade_id)
    remove_active_trade(trade_id)
    await write_event(trade_id, "resolution", trade["coin"], {})
    log.info("trade_resolved", trade_id=trade_id)
    if broadcast_fn:
        await broadcast_fn({"event": "trade_resolved", "trade_id": trade_id})
