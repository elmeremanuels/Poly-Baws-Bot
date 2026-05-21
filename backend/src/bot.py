"""Bot orchestration — runs the trading loop for all coins."""
import asyncio
from datetime import datetime, timezone

from . import scanner, ws_client, risk
from .config_loader import CONFIG
from .logger import log, write_event
from .state import (
    get_mode, is_paper_mode, is_auto_mode,
    get_active_count_by_coin, has_traded_window,
    create_trade_state, add_active_trade,
)
from .triggers import execute_entry

COINS = list(CONFIG["coins"].keys())
_broadcast_fn = None
_pending_hybrid_triggers: dict[str, asyncio.Event] = {}  # market_id -> Event


def set_broadcast_fn(fn) -> None:
    global _broadcast_fn
    _broadcast_fn = fn


def get_hybrid_pending() -> dict:
    return {k: v for k, v in _pending_hybrid_triggers.items()}


async def trigger_hybrid_entry(market_id: str) -> bool:
    """Called by dashboard to trigger a hybrid entry."""
    evt = _pending_hybrid_triggers.get(market_id)
    if evt:
        evt.set()
        log.info("hybrid_trigger_set", market_id=market_id)
        return True
    return False


async def _process_coin_window(coin: str, market: dict) -> None:
    """Try to enter a trade for a given coin/window."""
    active_counts = get_active_count_by_coin()
    ok, reason = await risk.pre_trade_checks(coin, active_counts)
    if not ok:
        log.info("trade_skipped_risk", coin=coin, reason=reason)
        return

    window_ts = market["window_start"].isoformat() if market["window_start"] else ""
    if has_traded_window(coin, window_ts):
        return

    mode = get_mode()
    trade = create_trade_state(coin, market, mode, triggered_by="bot")
    add_active_trade(trade)
    trade_id = trade["trade_id"]

    if not is_auto_mode():
        # Hybrid: register pending trigger and wait for user
        market_id = market.get("market_id") or market.get("condition_id")
        evt = asyncio.Event()
        _pending_hybrid_triggers[market_id] = evt
        if _broadcast_fn:
            await _broadcast_fn({
                "event": "hybrid_window_ready",
                "coin": coin,
                "market_id": market_id,
                "window_start": market["window_start"].isoformat() if market["window_start"] else None,
                "trade_id": trade_id,
            })
        log.info("hybrid_waiting", coin=coin, trade_id=trade_id, market_id=market_id)
        try:
            window_start = market["window_start"]
            if window_start:
                timeout = (window_start - datetime.now(timezone.utc)).total_seconds()
                from datetime import timedelta
                timeout -= CONFIG["trading"]["entry_cutoff_minutes_before_window"] * 60
            else:
                timeout = 600
            await asyncio.wait_for(evt.wait(), timeout=max(timeout, 10))
        except asyncio.TimeoutError:
            log.info("hybrid_trigger_timeout", coin=coin, market_id=market_id)
            from .state import remove_active_trade
            remove_active_trade(trade_id)
            _pending_hybrid_triggers.pop(market_id, None)
            return
        _pending_hybrid_triggers.pop(market_id, None)
        trade["triggered_by"] = "user"

    await write_event(trade_id, "entry_initiated", coin, {"mode": mode})
    success = await execute_entry(trade_id, broadcast_fn=_broadcast_fn)
    if not success:
        log.info("entry_failed", coin=coin, trade_id=trade_id)


async def _coin_loop(coin: str) -> None:
    """Per-coin trading loop."""
    while True:
        if risk.is_killed():
            log.info("coin_loop_killed", coin=coin)
            break
        try:
            market = scanner.get_tradeable_market(coin)
            if market:
                await _process_coin_window(coin, market)
        except Exception as e:
            log.error("coin_loop_error", coin=coin, error=str(e))
        await asyncio.sleep(10)


async def run_bot() -> None:
    """Start all coin loops and supporting tasks."""
    log.info("bot_starting", mode=get_mode(), coins=COINS)

    await scanner.refresh_markets()
    await ws_client.start()

    # Subscribe all known token IDs to WS
    all_tokens = []
    for coin in COINS:
        for m in scanner._market_cache.get(coin, []):
            if m.get("yes_token"):
                all_tokens.append(m["yes_token"])
            if m.get("no_token"):
                all_tokens.append(m["no_token"])
    if all_tokens:
        await ws_client.subscribe_assets(all_tokens)

    tasks = [
        asyncio.create_task(scanner.scanner_loop(60)),
        asyncio.create_task(risk.risk_monitor_loop(get_active_count_by_coin)),
    ]
    for coin in COINS:
        if CONFIG["coins"][coin]["enabled"]:
            tasks.append(asyncio.create_task(_coin_loop(coin)))

    await asyncio.gather(*tasks)
