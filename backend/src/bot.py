"""Bot orchestration — runs the trading loop for all coins."""
import asyncio
from datetime import datetime, timezone

from . import scanner, ws_client, risk, asset_price_feed, signals as _signals
from .config_loader import CONFIG
from .logger import log, write_event, save_dashboard_state
from .state import (
    get_mode, is_paper_mode, is_auto_mode,
    get_active_count_by_coin, has_traded_window,
    create_trade_state, add_active_trade,
)
from .triggers import execute_entry
from .commands import write_hybrid_pending, delete_hybrid_pending


def _effective_is_auto(mode: str) -> bool:
    """Live learning mode is always auto-triggered."""
    if mode == "live_learning":
        return True
    return is_auto_mode()


def _effective_is_paper(mode: str) -> bool:
    """In live learning mode, paper/live depends on current phase."""
    if mode == "live_learning":
        from . import learning as _learning
        return _learning.get_orchestrator().get_trading_mode().startswith("paper")
    return is_paper_mode()

COINS = list(CONFIG["coins"].keys())
_pending_hybrid_triggers: dict[str, asyncio.Event] = {}  # market_id -> Event


def _should_enter(market: dict) -> tuple[bool, str]:
    """Upgrade 3: reject entries with unfavourable combined cost or wide token spreads."""
    entry_cfg = CONFIG.get("entry", {})
    max_cost = entry_cfg.get("max_combined_cost", 1.03)
    max_spread = entry_cfg.get("max_token_spread", 0.06)

    yes_token = market.get("yes_token", "")
    no_token = market.get("no_token", "")
    yes_ask = ws_client.get_best_ask(yes_token)
    no_ask = ws_client.get_best_ask(no_token)
    yes_bid = ws_client.get_best_bid(yes_token)
    no_bid = ws_client.get_best_bid(no_token)

    if yes_ask is None or no_ask is None:
        return False, "no_orderbook_data"

    combined = yes_ask + no_ask
    if combined > max_cost:
        return False, f"combined_cost_too_high:{combined:.4f}"

    if yes_bid is not None and (yes_ask - yes_bid) > max_spread:
        return False, f"yes_spread_too_wide:{yes_ask - yes_bid:.4f}"
    if no_bid is not None and (no_ask - no_bid) > max_spread:
        return False, f"no_spread_too_wide:{no_ask - no_bid:.4f}"

    return True, "ok"


def get_hybrid_pending() -> dict:
    return dict(_pending_hybrid_triggers)


async def trigger_hybrid_entry(market_id: str) -> bool:
    """Called by command queue to trigger a hybrid entry."""
    evt = _pending_hybrid_triggers.get(market_id)
    if evt:
        evt.set()
        log.info("hybrid_trigger_set", market_id=market_id)
        return True
    return False


async def _process_coin_window(coin: str, market: dict) -> None:
    mode = get_mode()

    # Skip new entries during live_learning analysis phase
    if mode == "live_learning":
        from . import learning as _learning
        if _learning.is_analyzing():
            log.info("entry_skipped_analyzing", coin=coin)
            return

    active_counts = get_active_count_by_coin()
    ok, reason = await risk.pre_trade_checks(coin, active_counts, mode=mode)
    if not ok:
        log.info("trade_skipped_risk", coin=coin, reason=reason)
        return

    window_ts = market["window_start"].isoformat() if market["window_start"] else ""
    if has_traded_window(coin, window_ts):
        return

    # In auto mode: verify entry conditions BEFORE consuming the window slot.
    # If the check fails (e.g. no orderbook data yet), the window stays available
    # so the next _coin_loop iteration (10s later) can retry the same window.
    if _effective_is_auto(mode):
        ok_entry, entry_reason = _should_enter(market)
        if not ok_entry:
            log.info("trade_skipped_spread", coin=coin, reason=entry_reason)
            return  # window NOT registered — retried next iteration

    trade = create_trade_state(coin, market, mode, triggered_by="bot")

    # Stamp trade with current regime + price context
    from . import regime as _regime
    _regime_label = _regime.get_current_regime(coin)
    _bias = _regime.get_directional_bias(coin)
    _pstats = _regime.get_asset_price_stats(coin)
    trade["regime"] = _regime_label
    trade["directional_bias"] = _bias
    trade["price_position"] = _pstats["price_position"] if _pstats else None
    trade["asset_range_pct"] = _pstats["range_pct"] if _pstats else None

    # Stamp trade with current learning cycle info
    if mode == "live_learning":
        from . import learning as _learning
        import json as _json
        trade["cycle_id"] = _learning.get_current_cycle_id()
        trade["phase"] = _learning.get_current_phase()
        coin_cfg = CONFIG["coins"].get(coin, {})
        trade["param_snapshot"] = _json.dumps({
            "trigger_threshold": coin_cfg.get("trigger_threshold", CONFIG["trading"]["trigger_threshold"]),
            "cross_threshold": CONFIG.get("exit", {}).get("cross_threshold"),
            "initial_offset": CONFIG.get("exit", {}).get("initial_offset"),
        })

    add_active_trade(trade)  # registers window in _window_registry
    trade_id = trade["trade_id"]

    if not _effective_is_auto(mode):
        market_id = market.get("market_id") or market.get("condition_id")
        evt = asyncio.Event()
        _pending_hybrid_triggers[market_id] = evt

        # Persist to DB so Streamlit can show the trigger button
        write_hybrid_pending(
            market_id=market_id,
            coin=coin,
            window_start=market["window_start"].isoformat() if market["window_start"] else "",
            question=market.get("question", ""),
            trade_id=trade_id,
        )

        log.info("hybrid_waiting", coin=coin, trade_id=trade_id, market_id=market_id)
        try:
            window_start = market["window_start"]
            if window_start:
                from datetime import timedelta
                timeout = (window_start - datetime.now(timezone.utc)).total_seconds()
                timeout -= CONFIG["trading"]["entry_cutoff_minutes_before_window"] * 60
            else:
                timeout = 600
            await asyncio.wait_for(evt.wait(), timeout=max(timeout, 10))
        except asyncio.TimeoutError:
            log.info("hybrid_trigger_timeout", coin=coin, market_id=market_id)
            from .state import remove_active_trade
            remove_active_trade(trade_id)
            _pending_hybrid_triggers.pop(market_id, None)
            delete_hybrid_pending(market_id)
            return

        _pending_hybrid_triggers.pop(market_id, None)
        delete_hybrid_pending(market_id)
        trade["triggered_by"] = "user"

        # Hybrid mode: re-check spread after user clicks (prices may have moved)
        ok_entry, entry_reason = _should_enter(market)
        if not ok_entry:
            log.info("trade_skipped_spread_after_click", coin=coin, reason=entry_reason)
            from .state import remove_active_trade
            remove_active_trade(trade_id)
            return

    await write_event(trade_id, "entry_initiated", coin, {"mode": mode})
    success = await execute_entry(trade_id)
    if not success:
        log.info("entry_failed", coin=coin, trade_id=trade_id)


async def _coin_loop(coin: str) -> None:
    while True:
        if risk.is_killed():
            await asyncio.sleep(5)
            continue
        try:
            market = scanner.get_tradeable_market(coin)
            if market:
                await _process_coin_window(coin, market)
        except Exception as e:
            log.error("coin_loop_error", coin=coin, error=str(e))
        await asyncio.sleep(10)


async def _heartbeat_loop() -> None:
    """Write timestamp to DB every 5s so Streamlit can show bot-is-alive indicator."""
    while True:
        try:
            await save_dashboard_state("heartbeat", datetime.now(timezone.utc).isoformat())
        except Exception as e:
            log.warning("heartbeat_write_failed", error=str(e))
        await asyncio.sleep(5)


async def _portfolio_sync_loop() -> None:
    """Sync Polymarket balance and open positions to dashboard_state every 30s."""
    import json as _json
    from . import orders as _orders
    from .logger import load_dashboard_state

    first_run = True
    while True:
        try:
            balance = await _orders.get_balance()
            positions = await _orders.get_open_positions()

            total_pos_value = 0.0
            enriched = []
            for pos in positions:
                token_id = pos.get("asset") or pos.get("positionId") or ""
                size = float(pos.get("size") or 0)
                cur_price = pos.get("curPrice")
                cur_value = pos.get("currentValue")
                pnl = pos.get("pnl")
                value = float(cur_value) if cur_value is not None else None
                total_pos_value += value or 0
                enriched.append({
                    "token_id": token_id,
                    "title": pos.get("title", ""),
                    "outcome": pos.get("outcome", ""),
                    "size": size,
                    "avg_price": pos.get("avgPrice"),
                    "cur_price": float(cur_price) if cur_price is not None else None,
                    "value": round(value, 4) if value is not None else None,
                    "pnl": round(float(pnl), 4) if pnl is not None else None,
                })

            portfolio_value = (balance or 0.0) + total_pos_value

            if first_run:
                start = await load_dashboard_state("portfolio_start_usdc")
                if not start and balance is not None:
                    await save_dashboard_state(
                        "portfolio_start_usdc",
                        str(round(balance + total_pos_value, 4)),
                    )
                first_run = False

            await save_dashboard_state("portfolio_usdc", str(round(balance, 4)) if balance is not None else "")
            await save_dashboard_state("portfolio_positions", _json.dumps(enriched))
            await save_dashboard_state("portfolio_value", str(round(portfolio_value, 4)))
            await save_dashboard_state("portfolio_updated_at", datetime.now(timezone.utc).isoformat())
        except Exception as e:
            log.warning("portfolio_sync_failed", error=str(e))
        await asyncio.sleep(30)


async def _learning_tick_loop() -> None:
    """Drive the learning orchestrator — ticks every 10s to check phase transitions."""
    from . import learning as _learning
    orch = _learning.get_orchestrator()
    await orch.start()
    while True:
        try:
            await orch.tick()
        except Exception as e:
            log.error("learning_tick_error", error=str(e))
        await asyncio.sleep(10)


async def run_bot() -> None:
    log.info("bot_starting", mode=get_mode(), coins=COINS)

    await scanner.refresh_markets()

    all_tokens = []
    for coin in COINS:
        markets = scanner._market_cache.get(coin, [])
        log.info("bot_scanner_cache", coin=coin, markets=len(markets))
        for m in markets:
            if m.get("yes_token"):
                all_tokens.append(m["yes_token"])
            if m.get("no_token"):
                all_tokens.append(m["no_token"])

    log.info("bot_ws_subscribe_start", token_count=len(all_tokens))
    await ws_client.start()
    if all_tokens:
        await ws_client.subscribe_assets(all_tokens)
    else:
        log.warning("bot_no_tokens_no_orderbook_data_expected")

    tasks = [
        asyncio.create_task(scanner.scanner_loop(60)),
        asyncio.create_task(risk.risk_monitor_loop(get_active_count_by_coin)),
        asyncio.create_task(_heartbeat_loop()),
        asyncio.create_task(_portfolio_sync_loop()),
        asyncio.create_task(asset_price_feed.run()),
        asyncio.create_task(asset_price_feed.run_trade_stream()),
        asyncio.create_task(_signals.funding_rate_loop()),
    ]
    for coin in COINS:
        if CONFIG["coins"][coin]["enabled"]:
            tasks.append(asyncio.create_task(_coin_loop(coin)))

    if get_mode() == "live_learning":
        tasks.append(asyncio.create_task(_learning_tick_loop()))

    await asyncio.gather(*tasks)
