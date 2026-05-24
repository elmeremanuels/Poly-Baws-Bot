"""Live orderbook monitoring and trigger detection for active trades."""
import asyncio
from datetime import datetime, timezone, timedelta

from . import ws_client, paper_trader
from .config_loader import CONFIG
from .logger import log, write_event, write_snapshot
from .state import get_active_trades, update_trade_field

_DEFAULT_TRIGGER_THRESHOLD = CONFIG["trading"]["trigger_threshold"]
_monitoring_tasks: dict[str, asyncio.Task] = {}


async def start_monitoring(trade_id: str, on_trigger_callback) -> None:
    """Start monitoring an active trade for trigger condition."""
    if trade_id in _monitoring_tasks and not _monitoring_tasks[trade_id].done():
        return
    task = asyncio.create_task(_monitor_trade(trade_id, on_trigger_callback))
    _monitoring_tasks[trade_id] = task
    log.info("monitor_started", trade_id=trade_id)


async def stop_monitoring(trade_id: str) -> None:
    task = _monitoring_tasks.pop(trade_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    log.info("monitor_stopped", trade_id=trade_id)


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

            # Phase 1: stamp signals at trigger time
            from . import signals as _sig
            _trig_signals = _sig.get_all_signals(coin)
            update_trade_field(trade_id, "ofi_at_trigger", _trig_signals.get("ofi"))
            update_trade_field(trade_id, "funding_rate_at_trigger", _trig_signals.get("funding_rate"))
            update_trade_field(trade_id, "liq_proxy_at_trigger", _trig_signals.get("liq_proxy"))
            update_trade_field(trade_id, "conviction_at_trigger", _trig_signals.get("conviction"))
            update_trade_field(trade_id, "conviction_score_at_trigger", _trig_signals.get("conviction_score"))

            await on_trigger_callback(trade_id, winner, price)
            break

        # Periodic snapshot
        now_ts = loop.time()
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
