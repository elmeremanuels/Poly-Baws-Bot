"""Live orderbook monitoring and trigger detection for active trades."""
import asyncio
from datetime import datetime, timezone

from . import ws_client, paper_trader
from .config_loader import CONFIG
from .logger import log, write_event, write_snapshot
from .state import get_active_trades, update_trade_field

TRIGGER_THRESHOLD = CONFIG["trading"]["trigger_threshold"]
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
            await on_trigger_callback(trade_id, "RESOLUTION", None)
            break

        winner, price = paper_trader.check_trigger(yes_token, no_token, TRIGGER_THRESHOLD)
        if winner:
            log.info("trigger_detected", trade_id=trade_id, winner=winner, price=price)
            update_trade_field(trade_id, "trigger_hit", True)
            update_trade_field(trade_id, "trigger_ts", now.isoformat())
            update_trade_field(trade_id, "winner_side", winner)
            await write_event(trade_id, "trigger", trade["coin"], {"winner": winner, "price": price})
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
