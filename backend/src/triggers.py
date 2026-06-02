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

_SCALEIN_TRANCHES = 5            # split the scale-in budget into this many buys
_SCALEIN_MIN_EUR = 5.0           # minimum total scale-in budget when enabled
_scalein_state: dict[str, dict] = {}  # trade_id -> {count, ref_high, cycle_low}

_take_it_executed: set[str] = set()  # trade_ids where Take it already fired


def _get_trigger_threshold(coin: str) -> float:
    from . import regime as _regime
    return _regime.get_effective_trigger_threshold(coin)


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

    from . import regime as _regime
    from . import signals as _sig

    # ── Stamp Phase 1 signals at entry (refresh so data is ≤1s old) ──────────
    await _sig.refresh_ofi(coin)
    _entry_signals = _sig.get_all_signals(coin)
    _conv_dir: str | None = _entry_signals.get("conviction")
    _conv_score: float = float(_entry_signals.get("conviction_score") or 0.0)
    update_trade_field(trade_id, "ofi_at_entry", _entry_signals.get("ofi"))
    update_trade_field(trade_id, "funding_rate_at_entry", _entry_signals.get("funding_rate"))
    update_trade_field(trade_id, "liq_proxy_at_entry", _entry_signals.get("liq_proxy"))
    update_trade_field(trade_id, "conviction_at_entry", _conv_dir)
    update_trade_field(trade_id, "conviction_score_at_entry", _conv_score)

    # ── Conviction gate — check BEFORE placing any orders ────────────────────
    # Checking at trigger time (old design) was wrong: entry costs are already
    # paid by then. Check here so no orders are placed when signal is too weak.
    _ab_threshold = float(CONFIG.get("ab_test", {}).get("conviction_threshold", 0.0))
    if _ab_threshold > 0 and _conv_score < _ab_threshold:
        update_trade_field(trade_id, "status", "aborted")
        update_trade_field(trade_id, "notes", "conviction_below_threshold")
        update_trade_field(trade_id, "winner_exit_reason", "conviction_skip")
        await persist_trade(trade_id)
        remove_active_trade(trade_id)
        log.info("entry_conviction_skip", trade_id=trade_id,
                 score=round(_conv_score, 3), threshold=_ab_threshold)
        return False

    # ── Stamp regime at entry ─────────────────────────────────────────────────
    from .logger import get_recent_trades as _get_recent_trades
    _recent_trades = await _get_recent_trades(50)
    _coin_trades = [t for t in _recent_trades if t.get("coin") == coin]
    _regime_label = _regime.detect_regime(coin, _coin_trades)
    update_trade_field(trade_id, "regime_at_entry", _regime_label)
    update_trade_field(trade_id, "bias_direction_at_entry", _conv_dir)

    # ── paper flag + cutoff_time — needed by all paths below ────────────────
    trade_mode = trade.get("mode") or get_mode()
    if trade_mode == "live_learning":
        from . import learning as _learning
        paper = _learning.get_orchestrator().get_trading_mode().startswith("paper")
    else:
        paper = trade_mode.startswith("paper")

    window_start = datetime.fromisoformat(trade["window_start_ts"]).astimezone(timezone.utc)
    cutoff_time = window_start - timedelta(minutes=CUTOFF_MIN)

    # ── BGGDSB: single-side entry on dominant side only, no monitoring ──────
    # Must come before the past_cutoff check: BGGDSB enters DURING the window.
    if trade.get("router_bucket") == "bggdsb":
        _b_yes = trade.get("bggdsb_yes_shares")
        _b_no = trade.get("bggdsb_no_shares")
        if _b_yes is not None and _b_no is not None:
            update_trade_field(trade_id, "yes_size", float(_b_yes))
            update_trade_field(trade_id, "no_size", float(_b_no))
            dom_size = float(_b_yes) + float(_b_no)  # only one of them is non-zero
            update_trade_field(trade_id, "entry_size", round(dom_size, 2))
            update_trade_field(trade_id, "entry_type", "bggdsb")
            return await _execute_bggdsb_entry(trade_id, paper)

    now_utc = datetime.now(timezone.utc)
    if now_utc >= cutoff_time:
        update_trade_field(trade_id, "status", "aborted")
        update_trade_field(trade_id, "notes", "past_cutoff")
        await persist_trade(trade_id)
        remove_active_trade(trade_id)
        log.info("entry_aborted_past_cutoff", trade_id=trade_id,
                 seconds_past=round((now_utc - cutoff_time).total_seconds(), 1))
        return False

    # ── Conviction-weighted sizing ────────────────────────────────────────────
    # When conviction_weighting.enabled and score >= min_score, the biased side
    # gets up to max_ratio× the base size. Falls back to price_position bias
    # (RANGING only) when conviction_weighting is off.
    yes_size = size
    no_size = size
    _cw_cfg = CONFIG.get("conviction_weighting", {})
    if _cw_cfg.get("enabled") and _conv_dir and _conv_score >= float(_cw_cfg.get("min_score", 0.45)):
        _max_ratio = float(_cw_cfg.get("max_ratio", 2.0))
        _range = max(0.001, 1.0 - float(_cw_cfg.get("min_score", 0.45)))
        _weight = round(1.0 + (_conv_score - float(_cw_cfg.get("min_score", 0.45))) / _range * (_max_ratio - 1.0), 3)
        if _conv_dir == "UP":
            yes_size = round(size * _weight, 2)
        else:
            no_size = round(size * _weight, 2)
        log.info("conviction_weighted_entry", trade_id=trade_id, coin=coin,
                 direction=_conv_dir, score=round(_conv_score, 3), weight=_weight,
                 yes_size=yes_size, no_size=no_size)
    else:
        # Fallback: price_position bias in RANGING regime only
        _bias_cfg = CONFIG.get("bias", {})
        _bias, _certainty = _regime.get_bias_certainty(coin)
        if _bias and _certainty >= float(_bias_cfg.get("min_certainty", 0.20)):
            _max_ratio = float(_bias_cfg.get("max_weight_ratio", 2.0))
            _weight = round(1.0 + _certainty * (_max_ratio - 1.0), 3)
            if _bias == "UP":
                yes_size = round(size * _weight, 2)
            else:
                no_size = round(size * _weight, 2)
            log.info("price_position_weighted_entry", trade_id=trade_id, coin=coin,
                     bias=_bias, certainty=round(_certainty, 3), weight=_weight,
                     yes_size=yes_size, no_size=no_size)
    update_trade_field(trade_id, "yes_size", yes_size)
    update_trade_field(trade_id, "no_size", no_size)
    update_trade_field(trade_id, "bias_certainty", round(_conv_score, 3))

    # ── Phase 3: Tripartite entry routing ────────────────────────────────────
    _de_cfg = CONFIG.get("directional_entry", {})
    _dir_enabled = _de_cfg.get("enabled", False)
    _dir_threshold = float(_de_cfg.get("conviction_threshold", 0.70))
    _straddle_min = float(_de_cfg.get("straddle_min_conviction", 0.0))

    # Skip: conviction present but too weak for straddle
    if _straddle_min > 0 and 0 < _conv_score < _straddle_min:
        update_trade_field(trade_id, "status", "aborted")
        update_trade_field(trade_id, "notes", "conviction_too_low_skip")
        update_trade_field(trade_id, "entry_type", "skipped")
        update_trade_field(trade_id, "winner_exit_reason", "conviction_skip")
        await persist_trade(trade_id)
        remove_active_trade(trade_id)
        log.info("entry_skip_low_conviction", trade_id=trade_id,
                 score=round(_conv_score, 3), min_required=_straddle_min)
        return False

    # Directional: high conviction + known direction
    if _dir_enabled and _conv_dir and _conv_score >= _dir_threshold:
        update_trade_field(trade_id, "entry_type", "directional")
        return await _execute_directional_entry(
            trade_id, _conv_dir, paper, cutoff_time, broadcast_fn
        )

    # Straddle (default)
    update_trade_field(trade_id, "entry_type", "straddle")
    return await _execute_straddle_orders(trade_id, paper, cutoff_time, broadcast_fn)


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


async def _sell_loser_immediately(
    loser_token: str,
    size: float,
    paper: bool,
    coin: str,
    trade_id: str,
) -> dict:
    """Sell loser via immediate market order (taker fill at current bid)."""
    mid = ws_client.get_mid_price(loser_token) or 0.30
    log.info("loser_market_sell", trade_id=trade_id, coin=coin, mid=round(mid, 4))

    if paper:
        return await paper_trader.simulate_market_sell(loser_token, size)

    market_resp = await orders.place_market_order(loser_token, "SELL", size)
    if market_resp and market_resp.get("order_id"):
        fill = await _fetch_market_fill(
            market_resp["order_id"], fallback=round(mid * 0.7, 2), size=size
        )
        return {"filled": True, "fill_price": fill["fill_price"], "fees": fill["fees"]}
    return {"filled": False, "fill_price": round(mid * 0.7, 2), "fees": 0.0}


async def _sell_loser_with_limit(
    loser_token: str,
    size: float,
    paper: bool,
    coin: str,
    trade_id: str,
    seconds_left: float = 999.0,
) -> dict:
    """Try passive limit sell on loser before falling back to market sell.

    Places a resting limit at effective_bid + bid_buffer, undercutting all
    existing asks.  Any incoming buyer fills against our order rather than
    the deeper book, capturing bid_buffer extra cents per share.

    Paper mode and near-window-end situations skip straight to market sell
    (paper simulation cannot model future buyers filling resting orders).
    """
    cfg = CONFIG.get("exit", {})
    min_secs = float(cfg.get("pre_exit_limit_min_seconds_left", 90.0))

    if paper or seconds_left < min_secs:
        return await _sell_loser_immediately(loser_token, size, paper, coin, trade_id)

    bid_buffer = float(cfg.get("pre_exit_limit_bid_buffer", 0.04))
    timeout_secs = float(cfg.get("pre_exit_limit_timeout", 20.0))

    eff_bid = _estimate_sell_fill(loser_token, size) or ws_client.get_best_bid(loser_token)
    mid = ws_client.get_mid_price(loser_token) or 0.30

    if eff_bid is None:
        return await _sell_loser_immediately(loser_token, size, paper, coin, trade_id)

    limit_price = round(max(0.05, min(0.94, eff_bid + bid_buffer)), 2)

    log.info(
        "loser_limit_sell_attempt",
        trade_id=trade_id, coin=coin,
        eff_bid=round(eff_bid, 4), mid=round(mid, 4),
        limit=limit_price, timeout=timeout_secs,
    )

    limit_resp = await orders.place_limit_order(loser_token, "SELL", limit_price, size)
    if not limit_resp or not limit_resp.get("order_id"):
        log.warning("loser_limit_place_failed_market_fallback", trade_id=trade_id)
        return await _sell_loser_immediately(loser_token, size, paper, coin, trade_id)

    limit_order_id = limit_resp["order_id"]
    deadline = asyncio.get_event_loop().time() + timeout_secs

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(1.5)
        order = await orders.get_order(limit_order_id)
        if order:
            status = order.get("status")
            if status in ("MATCHED", "FILLED"):
                avg = float(order.get("average_price") or order.get("price") or limit_price)
                log.info(
                    "loser_limit_filled_live",
                    trade_id=trade_id, fill_price=avg, limit=limit_price,
                    improvement=round(avg - eff_bid, 4),
                )
                return {"filled": True, "fill_price": avg, "fees": 0.0}  # maker fill = zero fee
            if status in ("CANCELED", "UNMATCHED"):
                log.warning("loser_limit_cancelled_market_fallback",
                            trade_id=trade_id, status=status)
                return await _sell_loser_immediately(loser_token, size, paper, coin, trade_id)

    # Timeout: cancel resting limit then market sell
    await orders.cancel_order(limit_order_id)
    log.info("loser_limit_timeout_market_fallback",
             trade_id=trade_id, limit_order_id=limit_order_id, limit=limit_price)
    return await _sell_loser_immediately(loser_token, size, paper, coin, trade_id)


async def _fetch_market_fill(order_id: str, fallback: float, size: float) -> dict:
    """Poll once after a FOK market sell to get actual fill price and fees.
    FOK orders fill or die immediately, so one pass with a 1.5s grace period suffices."""
    for attempt in range(3):
        await asyncio.sleep(1.0 if attempt == 0 else 0.5)
        order = await orders.get_order(order_id)
        if order:
            status = order.get("status")
            if status in ("MATCHED", "FILLED"):
                avg = float(order.get("average_price") or order.get("price") or fallback)
                fees = float(order.get("fees_charged") or order.get("fees") or 0.0)
                if fees == 0.0:
                    fees = round(paper_trader.taker_fee_rate(avg) * avg * size, 6)
                log.info("market_fill_confirmed", order_id=order_id, avg_price=avg, fees=fees)
                return {"fill_price": avg, "fees": fees}
            if status in ("CANCELED", "UNMATCHED"):
                break
    log.warning("market_fill_fallback", order_id=order_id, fallback=fallback)
    return {"fill_price": fallback, "fees": round(paper_trader.taker_fee_rate(fallback) * fallback * size, 6)}


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


async def _execute_bggdsb_entry(trade_id: str, paper: bool) -> bool:
    """Buy only the dominant side for a BGGDSB trade. No monitoring started.

    Fixes the straddle path bug where no_size=0.0 was coerced to entry_size
    via Python's `or` chain, accidentally buying the non-dominant side.
    """
    trade = get_active_trades().get(trade_id)
    if not trade:
        return False

    coin = trade["coin"]
    dominant_side = trade.get("bggdsb_dominant_side") or "YES"
    yes_token = trade["condition_id_yes"]
    no_token  = trade["condition_id_no"]
    dom_token = yes_token if dominant_side == "YES" else no_token

    dom_size = float(
        trade.get("yes_size") if dominant_side == "YES" else trade.get("no_size") or 0.0
    )
    if dom_size <= 0:
        log.warning("bggdsb_entry_zero_size", trade_id=trade_id, dominant_side=dominant_side)
        update_trade_field(trade_id, "status", "aborted")
        update_trade_field(trade_id, "notes", "bggdsb_zero_size")
        await persist_trade(trade_id)
        remove_active_trade(trade_id)
        return False

    update_trade_field(trade_id, "entry_placed_ts", datetime.now(timezone.utc).isoformat())
    update_trade_field(trade_id, "status", "entry_placed")

    if paper:
        ask = ws_client.get_best_ask(dom_token) or ENTRY_PRICE
        result = await paper_trader.simulate_limit_buy(dom_token, ask, dom_size)
        if not result.get("filled"):
            # WS order book empty (bot startup) — simulate fill at ask price
            result = {"filled": True, "fill_price": ask, "filled_size": dom_size, "fees": 0.0}
    else:
        ask = round(ws_client.get_best_ask(dom_token) or ENTRY_PRICE, 2)
        resp = await orders.place_limit_order(dom_token, "BUY", ask, dom_size)
        if resp and resp.get("order_id"):
            cutoff = datetime.now(timezone.utc) + timedelta(seconds=30)
            result = await _poll_live_fill(resp["order_id"], cutoff)
        else:
            result = {"filled": False}

    if result.get("filled"):
        fill_price = result.get("fill_price") or ask
        fees = result.get("fees") or 0.0
        now_ts = datetime.now(timezone.utc).isoformat()
        update_trade_field(trade_id, "entry_filled_ts", now_ts)
        update_trade_field(trade_id, "trigger_hit", True)
        update_trade_field(trade_id, "trigger_ts", now_ts)
        if dominant_side == "YES":
            update_trade_field(trade_id, "entry_yes_price", fill_price)
            update_trade_field(trade_id, "entry_no_price", None)
        else:
            update_trade_field(trade_id, "entry_yes_price", None)
            update_trade_field(trade_id, "entry_no_price", fill_price)
        update_trade_field(trade_id, "fees_paid", fees)
        update_trade_field(trade_id, "status", "monitoring")
        await persist_trade(trade_id)
        log.info("bggdsb_entry_filled", trade_id=trade_id, coin=coin,
                 dominant_side=dominant_side, fill_price=round(fill_price, 4),
                 dom_size=dom_size, paper=paper)
        return True
    else:
        update_trade_field(trade_id, "status", "aborted")
        update_trade_field(trade_id, "notes", "bggdsb_entry_no_fill")
        await persist_trade(trade_id)
        remove_active_trade(trade_id)
        log.warning("bggdsb_entry_no_fill", trade_id=trade_id)
        return False


async def _execute_straddle_orders(
    trade_id: str, paper: bool, cutoff_time: datetime, broadcast_fn
) -> bool:
    """Place YES+NO limit buy orders and wait for fills (straddle path)."""
    trade = get_active_trades().get(trade_id)
    if not trade:
        return False
    coin = trade["coin"]
    yes_token = trade["condition_id_yes"]
    no_token = trade["condition_id_no"]
    yes_size = float(trade.get("yes_size") or trade.get("entry_size") or 2)
    no_size = float(trade.get("no_size") or trade.get("entry_size") or 2)

    log.info("entry_starting", trade_id=trade_id, coin=coin, paper=paper)
    await write_event(trade_id, "entry_start", coin, {"paper": paper, "size": float(trade.get("entry_size", 2))})
    update_trade_field(trade_id, "entry_placed_ts", datetime.now(timezone.utc).isoformat())
    update_trade_field(trade_id, "status", "entry_placed")

    if paper:
        yes_ask = ws_client.get_best_ask(yes_token) or ENTRY_PRICE
        no_ask = ws_client.get_best_ask(no_token) or ENTRY_PRICE
        yes_result, no_result = await asyncio.gather(
            paper_trader.simulate_limit_buy(yes_token, yes_ask, yes_size),
            paper_trader.simulate_limit_buy(no_token, no_ask, no_size),
        )
        if not yes_result["filled"] or not no_result["filled"]:
            log.warning("entry_limit_fill_failed", trade_id=trade_id,
                        yes_filled=yes_result["filled"], no_filled=no_result["filled"],
                        yes_ask=yes_ask, no_ask=no_ask)
    else:
        yes_ask = round(ws_client.get_best_ask(yes_token) or ENTRY_PRICE, 2)
        no_ask = round(ws_client.get_best_ask(no_token) or ENTRY_PRICE, 2)
        yes_resp, no_resp = await asyncio.gather(
            orders.place_limit_order(yes_token, "BUY", yes_ask, yes_size),
            orders.place_limit_order(no_token, "BUY", no_ask, no_size),
        )
        if yes_resp:
            update_trade_field(trade_id, "yes_order_id", yes_resp["order_id"])
        if no_resp:
            update_trade_field(trade_id, "no_order_id", no_resp["order_id"])
        yes_result = await _poll_live_fill(yes_resp["order_id"] if yes_resp else None, cutoff_time)
        no_result = await _poll_live_fill(no_resp["order_id"] if no_resp else None, cutoff_time)

    yes_filled = yes_result.get("filled", False)
    no_filled = no_result.get("filled", False)
    await _handle_fill_results(trade_id, yes_filled, no_filled, yes_result, no_result, paper, broadcast_fn)
    return yes_filled and no_filled


async def _execute_directional_entry(
    trade_id: str,
    direction: str,
    paper: bool,
    cutoff_time: datetime,
    broadcast_fn,
) -> bool:
    """Buy only the biased side when conviction >= directional_threshold.

    Lifecycle:
      - Enter at ~50¢ (one side only — half the straddle cost)
      - If correct direction triggers → trail winner with peg-cross to $1.00
      - If wrong direction triggers → sell our position at market
      - Break-even ≈ entry price; expected win P&L ≈ €0.50/share vs straddle's ~€0.30
    Falls back to straddle on depth or pricing-model failure.
    """
    trade = get_active_trades().get(trade_id)
    if not trade:
        return False

    coin = trade["coin"]
    yes_token = trade["condition_id_yes"]
    no_token = trade["condition_id_no"]
    size = float(trade.get("entry_size") or 2)
    side_name = "YES" if direction == "UP" else "NO"
    token = yes_token if side_name == "YES" else no_token

    de_cfg = CONFIG.get("directional_entry", {})

    # ── Pricing model edge check (live only) ─────────────────────────────────
    pm_cfg = de_cfg.get("pricing_model", {})
    if pm_cfg.get("enabled", False) and not paper:
        from . import signals as _sig
        window_start = datetime.fromisoformat(trade["window_start_ts"]).astimezone(timezone.utc)
        t_remaining = max(10.0, (window_start - datetime.now(timezone.utc)).total_seconds() + 300.0)
        polymarket_mid = ws_client.get_mid_price(token) or 0.50
        edge = _sig.get_edge(coin, side_name, polymarket_mid, t_remaining)
        min_edge = float(pm_cfg.get("min_edge", 0.04))
        # Stamp pricing model results regardless
        theo_yes = _sig.theoretical_price("YES", coin, t_remaining)
        theo_no = _sig.theoretical_price("NO", coin, t_remaining)
        update_trade_field(trade_id, "theoretical_price_yes", theo_yes)
        update_trade_field(trade_id, "theoretical_price_no", theo_no)
        update_trade_field(trade_id, "edge_at_entry", edge)
        if edge is not None and edge < min_edge:
            log.info("directional_edge_insufficient", trade_id=trade_id, coin=coin,
                     direction=direction, edge=edge, min_edge=min_edge)
            update_trade_field(trade_id, "entry_type", "straddle_no_edge_fallback")
            return await _execute_straddle_orders(trade_id, paper, cutoff_time, broadcast_fn)

    # ── Liquidity depth check (live only) ────────────────────────────────────
    min_depth = float(de_cfg.get("min_depth_shares", 20))
    if not paper:
        book = ws_client.get_orderbook(token)
        asks = sorted([(float(p), float(s)) for p, s in book.get("asks", {}).items()])
        top3_depth = sum(s for _, s in asks[:3])
        if top3_depth < min_depth:
            log.info("directional_depth_insufficient", trade_id=trade_id, coin=coin,
                     direction=direction, depth=round(top3_depth, 1), min_depth=min_depth)
            update_trade_field(trade_id, "entry_type", "straddle_depth_fallback")
            return await _execute_straddle_orders(trade_id, paper, cutoff_time, broadcast_fn)

    # ── Place single-side limit buy ───────────────────────────────────────────
    ask = round(ws_client.get_best_ask(token) or ENTRY_PRICE, 2)
    log.info("directional_entry_starting", trade_id=trade_id, coin=coin,
             direction=direction, side=side_name, ask=ask, size=size)
    await write_event(trade_id, "directional_entry_start", coin,
                      {"direction": direction, "side": side_name, "ask": ask})
    update_trade_field(trade_id, "entry_placed_ts", datetime.now(timezone.utc).isoformat())
    update_trade_field(trade_id, "status", "entry_placed")
    update_trade_field(trade_id, "directional_side", side_name)

    if paper:
        result = await paper_trader.simulate_limit_buy(token, ask, size)
    else:
        resp = await orders.place_limit_order(token, "BUY", ask, size)
        order_id = resp["order_id"] if resp else None
        update_trade_field(trade_id, "yes_order_id" if side_name == "YES" else "no_order_id", order_id)
        result = await _poll_live_fill(order_id, cutoff_time)

    if not result.get("filled"):
        if not paper:
            oid = trade.get("yes_order_id" if side_name == "YES" else "no_order_id")
            if oid:
                await orders.cancel_order(oid)
        update_trade_field(trade_id, "status", "aborted")
        update_trade_field(trade_id, "notes", "directional_no_fill")
        await persist_trade(trade_id)
        remove_active_trade(trade_id)
        log.info("directional_entry_no_fill", trade_id=trade_id)
        return False

    fill_price = result.get("fill_price", ask)
    fees = result.get("fees", 0.0)

    # The side we didn't buy has price=0 and size=0 — _close_trade P&L math works correctly
    if side_name == "YES":
        update_trade_field(trade_id, "entry_yes_price", fill_price)
        update_trade_field(trade_id, "entry_no_price", 0.0)
        update_trade_field(trade_id, "yes_size", size)
        update_trade_field(trade_id, "no_size", 0.0)
    else:
        update_trade_field(trade_id, "entry_yes_price", 0.0)
        update_trade_field(trade_id, "entry_no_price", fill_price)
        update_trade_field(trade_id, "yes_size", 0.0)
        update_trade_field(trade_id, "no_size", size)

    update_trade_field(trade_id, "fees_paid", fees)
    update_trade_field(trade_id, "entry_filled_ts", datetime.now(timezone.utc).isoformat())
    update_trade_field(trade_id, "status", "monitoring")
    await write_event(trade_id, "entry_filled_directional", coin,
                      {"direction": direction, "side": side_name, "fill_price": fill_price, "size": size})
    await persist_trade(trade_id)
    log.info("directional_entry_filled", trade_id=trade_id, coin=coin,
             side=side_name, fill_price=fill_price, size=size)
    if broadcast_fn:
        await broadcast_fn({"event": "entry_filled", "trade_id": trade_id})
    await start_monitoring(trade_id, lambda tid, w, p: on_trigger(tid, w, p, broadcast_fn))
    return True


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

    trade = get_active_trades().get(trade_id)
    if not trade:
        return

    # Route directional trades to their own handler (handles both trigger and resolution)
    if trade.get("entry_type") == "directional":
        await _on_trigger_directional(trade_id, winner, broadcast_fn)
        return

    if winner == "RESOLUTION":
        await _handle_resolution(trade_id, broadcast_fn)
        return

    # Conviction gate is now enforced at entry time (execute_entry), not here.
    # Trades that reach on_trigger always proceed to exit.
    update_trade_field(trade_id, "ab_group", "A")

    # Use the mode stored at trade creation time so a mode change mid-trade
    # doesn't switch between paper/live execution.
    trade_mode = trade.get("mode") or get_mode()
    if trade_mode == "live_learning":
        from . import learning as _learning
        paper = _learning.get_orchestrator().get_trading_mode().startswith("paper")
    else:
        paper = trade_mode.startswith("paper")
    coin = trade["coin"]

    # Stamp Signal Lab fields at trigger time (trigger_hit / winner_side were never
    # persisted to the DB for straddle trades — only broadcast as events).
    from . import signals as _sig_trig
    _trig_sigs = _sig_trig.get_all_signals(coin)
    update_trade_field(trade_id, "trigger_hit", True)
    update_trade_field(trade_id, "winner_side", winner)
    update_trade_field(trade_id, "conviction_at_trigger",       _trig_sigs.get("conviction"))
    update_trade_field(trade_id, "conviction_score_at_trigger", float(_trig_sigs.get("conviction_score") or 0.0))
    update_trade_field(trade_id, "ofi_at_trigger",              _trig_sigs.get("ofi"))
    update_trade_field(trade_id, "funding_rate_at_trigger",     _trig_sigs.get("funding_rate"))
    update_trade_field(trade_id, "liq_proxy_at_trigger",        _trig_sigs.get("liq_proxy"))
    yes_token = trade["condition_id_yes"]
    no_token = trade["condition_id_no"]
    size = trade["entry_size"]
    yes_size = float(trade.get("yes_size") or size)
    no_size = float(trade.get("no_size") or size)

    loser_side = "NO" if winner == "YES" else "YES"
    loser_token = no_token if loser_side == "NO" else yes_token
    winner_token = yes_token if winner == "YES" else no_token
    loser_size = no_size if loser_side == "NO" else yes_size
    winner_size = yes_size if winner == "YES" else no_size

    # Directional bias adjustment: RANGING regime with a bias signal.
    # With-bias win: lower cross_threshold (more patient, expect larger move).
    # Against-bias win: raise cross_threshold (take profit faster, expect reversal).
    bias_cross_adj = 0.0
    trade_regime = trade.get("regime", "UNKNOWN")
    trade_bias = trade.get("directional_bias")  # "UP", "DOWN", or None
    if trade_regime == "RANGING" and trade_bias:
        with_bias = (trade_bias == "DOWN" and winner == "NO") or \
                    (trade_bias == "UP" and winner == "YES")
        bias_cross_adj = -0.10 if with_bias else +0.15
        log.info("bias_cross_adj", trade_id=trade_id, regime=trade_regime,
                 bias=trade_bias, winner=winner, with_bias=with_bias, adj=bias_cross_adj)

    window_end = trade.get("window_end_ts")
    window_end_dt = datetime.fromisoformat(window_end).astimezone(timezone.utc) if window_end else None
    seconds_to_end = (window_end_dt - datetime.now(timezone.utc)).total_seconds() if window_end_dt else 999.0

    log.info("executing_trigger_action", trade_id=trade_id, winner=winner, loser=loser_side,
             seconds_to_end=round(seconds_to_end, 1))

    # ── Early loser sell resolution ───────────────────────────────────────────
    early_side = trade.get("early_loser_side")
    early_price = float(trade.get("early_loser_price") or 0.0)
    early_rebought = trade.get("early_loser_rebought")

    if early_side and not early_rebought:
        if early_side == loser_side:
            # Correct: early sell captured the actual loser at a much better price
            loser_price = early_price
            loser_fees = 0.0
            log.info("early_loser_confirmed_correct",
                     trade_id=trade_id, side=early_side, fill_price=loser_price)
        else:
            # Wrong side: we sold the eventual winner early
            # Sell the actual loser (still in wallet), use early fill as winner exit
            log.warning("early_loser_was_winner",
                        trade_id=trade_id, sold=early_side, actual_winner=winner,
                        early_price=early_price)
            loser_result = await _sell_loser_with_limit(
                loser_token, loser_size, paper, coin, trade_id, seconds_left=seconds_to_end
            )
            loser_price = loser_result.get("fill_price") or 0.17
            loser_fees = loser_result.get("fees") or 0.0
            update_trade_field(trade_id, "loser_exit_price", loser_price)
            update_trade_field(trade_id, "loser_exit_ts", datetime.now(timezone.utc).isoformat())
            # Winner was sold early — record that price and close without trailing
            entry_yes = trade.get("entry_yes_price") or ENTRY_PRICE
            entry_no = trade.get("entry_no_price") or ENTRY_PRICE
            fees_total = (trade.get("fees_paid") or 0.0) + loser_fees
            cost = entry_yes * yes_size + entry_no * no_size + fees_total
            proceeds = early_price * winner_size + loser_price * loser_size
            update_trade_field(trade_id, "winner_exit_price", early_price)
            update_trade_field(trade_id, "winner_exit_reason", "early_loser_was_winner")
            update_trade_field(trade_id, "actual_winner", winner)
            update_trade_field(trade_id, "fees_paid", fees_total)
            update_trade_field(trade_id, "gross_pnl", round(proceeds - cost + fees_total, 4))
            update_trade_field(trade_id, "net_pnl", round(proceeds - cost, 4))
            update_trade_field(trade_id, "status", "closed")
            await persist_trade(trade_id)
            remove_active_trade(trade_id)
            if broadcast_fn:
                await broadcast_fn({"event": "trade_closed", "trade_id": trade_id,
                                    "reason": "early_loser_was_winner"})
            return
    else:
        # No early sell, or early sell was rebought (trade back to normal) — standard exit
        loser_result = await _sell_loser_with_limit(
            loser_token, loser_size, paper, coin, trade_id, seconds_left=seconds_to_end
        )
        loser_price = loser_result.get("fill_price") or 0.30
        loser_fees = loser_result.get("fees") or 0.0

    update_trade_field(trade_id, "loser_exit_price", loser_price)
    update_trade_field(trade_id, "loser_exit_ts", datetime.now(timezone.utc).isoformat())

    await write_event(trade_id, "loser_sold", coin, {
        "side": loser_side, "price": loser_price,
        "loser_size": loser_size, "winner_size": winner_size,
    })

    current_fees = trade.get("fees_paid") or 0.0
    total_fees_so_far = current_fees + loser_fees
    update_trade_field(trade_id, "fees_paid", total_fees_so_far)

    # Break-even accounts for asymmetric position sizes
    entry_yes = trade.get("entry_yes_price") or ENTRY_PRICE
    entry_no = trade.get("entry_no_price") or ENTRY_PRICE
    total_cost = entry_yes * yes_size + entry_no * no_size + total_fees_so_far
    loser_proceeds = loser_price * loser_size
    be_price = max(0.0, min(1.0, round((total_cost - loser_proceeds) / winner_size, 4)))
    update_trade_field(trade_id, "break_even_price", be_price)
    update_trade_field(trade_id, "actual_winner", winner)
    await write_event(trade_id, "break_even_computed", coin, {
        "break_even_price": be_price, "winner_size": winner_size, "loser_size": loser_size,
    })

    # Step 2: trail winner with its actual (possibly weighted) size
    asyncio.create_task(_winner_exit_oco(trade_id, winner_token, winner_size, paper, broadcast_fn, bias_cross_adj))
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
    bias_cross_adj: float = 0.0,
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
        await _winner_exit_paper(trade_id, winner_token, size, window_end_dt, broadcast_fn, bias_cross_adj)
    else:
        await _winner_exit_live(trade_id, winner_token, size, window_end_dt, broadcast_fn, bias_cross_adj)


def _store_trail_metrics(trade_id: str, peak_bid: float, ratchet_count: int, time_in_trail: float) -> None:
    update_trade_field(trade_id, "peak_bid", round(peak_bid, 4))
    update_trade_field(trade_id, "ratchet_count", ratchet_count)
    update_trade_field(trade_id, "time_in_trail_seconds", round(time_in_trail, 2))


def _add_winner_fees(trade_id: str, fees: float) -> None:
    current = (get_active_trades().get(trade_id) or {}).get("fees_paid") or 0.0
    update_trade_field(trade_id, "fees_paid", round(current + fees, 6))


# ── Peg-Cross Exit Engine ─────────────────────────────────────────────────────

def _estimate_sell_fill(token_id: str, size: float) -> float | None:
    """Walk live bid book to estimate average fill price for a market sell of `size` shares.

    Returns None if the book is empty. Falls back to best_bid when book has less depth than needed.
    This gives a more realistic crossing cost than assuming full fill at best_bid.
    """
    book = ws_client.get_orderbook(token_id)
    bids = sorted([(float(p), s) for p, s in book["bids"].items()], key=lambda x: -x[0])
    if not bids:
        return None
    filled, cost = 0.0, 0.0
    for price, avail in bids:
        take = min(float(avail), size - filled)
        cost += take * price
        filled += take
        if filled >= size:
            break
    return cost / filled if filled > 0 else None


def crossing_cost(mid: float, spread: float, size: float, effective_bid: float | None = None) -> float:
    """Cost (€) of converting a resting limit sell to a market sell.

    Uses effective_bid when provided (depth-aware estimate from walking the book);
    otherwise falls back to mid - spread/2, which equals best_bid when mid = (bid+ask)/2.
    """
    bid = max(0.01, effective_bid if effective_bid is not None else mid - spread / 2)
    taker = bid * paper_trader.taker_fee_rate(bid) * size
    spread_loss = (mid - bid) * size  # gap between mid and actual execution price
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
    effective_bid: float | None = None,
) -> tuple[float, str]:
    """
    Returns (score 0–1, dominant_reason). score ≥ cross_threshold → market sell.

    Components:
      0.30 time_urgency         — rises as window end approaches
      0.30 fee_adjusted         — expected peg loss vs crossing cost (depth-aware)
      0.25 mid_decay            — mid dropped from peak → momentum reversed
      0.15 fill_unlikely        — limit above best ask → passive fill impossible
    """
    urgency_window = cfg.get("time_urgency_window", 120)
    time_urgency = max(0.0, min(1.0, 1.0 - seconds_left / urgency_window))

    spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else 0.06

    if velocity < 0:
        cost = crossing_cost(mid, spread, size, effective_bid=effective_bid)
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
        urgent_threshold = cfg.get("urgent_cross_threshold", 0.50)
        return {"cross_threshold": urgent_threshold, "check_interval": max(0.5, base_interval / 2), "ratchet_buffer": base_buffer}
    return {"cross_threshold": 0.0, "check_interval": 0.2, "ratchet_buffer": 0.0}


async def _check_and_execute_take_it(
    trade_id: str,
    winner_token: str,
    mid: float,
    seconds_left: float,
    size: float,
    break_even_price: float | None,
    paper: bool,
    coin: str,
) -> float:
    """Buy extra winner tokens when the market is clearly heading for a $1 resolution.

    Returns the updated position size (original + extra shares bought, or original if not triggered).
    Max 1x per trade. Triggers when: mid ≥ 0.82, seconds_left ≤ 240,
    theoretical_price ≥ 0.85, and take_it is enabled.
    """
    cfg = CONFIG.get("router", {})
    if not cfg.get("take_it_enabled", True):
        return size
    if trade_id in _take_it_executed:
        return size
    if mid < 0.82 or seconds_left > 240:
        return size

    # Black-Scholes confirmation
    from .signals import theoretical_price as _theo
    theo = _theo("YES", coin, seconds_left)  # side doesn't matter; YES/NO tokens are symmetric
    if theo is None or theo < 0.85:
        return size

    take_size_eur = float(cfg.get("take_it_size_eur", 3.0))
    extra_shares = round(take_size_eur / mid, 2)
    if extra_shares < 0.10:
        return size

    _take_it_executed.add(trade_id)

    if paper:
        result = await paper_trader.simulate_market_buy(winner_token, extra_shares)
    else:
        result = await orders.place_market_order(winner_token, "BUY", extra_shares)
        if result and result.get("order_id"):
            f = await _fetch_market_fill(result["order_id"], fallback=mid, size=extra_shares)
            result = {"filled": True, "fill_price": f["fill_price"], "fees": f["fees"],
                      "filled_size": extra_shares}

    if not (result and result.get("filled")):
        _take_it_executed.discard(trade_id)
        return size

    fill_price = result.get("fill_price") or mid
    filled_sz = result.get("filled_size") or extra_shares
    si_cost = fill_price * filled_sz + result.get("fees", 0.0)
    new_size = round(size + filled_sz, 4)

    if break_even_price is not None and break_even_price > 0:
        new_be = round((break_even_price * size + si_cost) / new_size, 4)
    else:
        new_be = break_even_price

    update_trade_field(trade_id, "take_it_executed", 1)
    _add_winner_fees(trade_id, result.get("fees", 0.0))
    log.info("take_it_executed", trade_id=trade_id, coin=coin, mid=round(mid, 4),
             extra_shares=filled_sz, fill_price=round(fill_price, 4),
             new_size=new_size, seconds_left=round(seconds_left, 1),
             theo=round(theo, 3), paper=paper)
    await write_event(trade_id, "take_it_executed", coin, {
        "mid": round(mid, 4), "extra_shares": filled_sz, "fill_price": round(fill_price, 4),
        "new_size": new_size, "new_break_even": new_be, "theo": round(theo, 3),
    })
    return new_size


async def _winner_exit_paper(
    trade_id: str,
    winner_token: str,
    size: float,
    window_end_dt: datetime | None,
    broadcast_fn,
    bias_cross_adj: float = 0.0,
) -> None:
    """Paper mode: peg-cross exit engine — resting limit + dynamic market conversion."""
    trade = get_active_trades().get(trade_id)
    coin = trade["coin"] if trade else "UNKNOWN"
    trade_regime = (trade.get("regime") if trade else None) or "UNKNOWN"
    es = _vol.get_coin_params(coin)
    if bias_cross_adj:
        es["cross_threshold"] = max(0.10, min(0.95, es["cross_threshold"] + bias_cross_adj))
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
    # Pre-compute loss-control constants (stable per trade)
    _max_loss_raw = es.get("winner_max_loss_per_trade_eur", 0.70)
    _max_loss_eur = float(_max_loss_raw) if _max_loss_raw is not None else None  # None = disabled
    _vel_raw = es.get("winner_velocity_stop", -0.004)
    _vel_stop = float(_vel_raw) if _vel_raw is not None else None  # None = disabled (e.g. RANGING)
    _winner_stop_bid = (
        round(break_even_price - _max_loss_eur / max(0.01, size), 4)
        if (break_even_price and _max_loss_eur is not None)
        else None  # None = stop disabled
    )

    await write_event(trade_id, "trailing_started", coin, {
        "initial_limit": current_limit, "trigger_price": trigger_price,
        "break_even_price": break_even_price, "winner_stop_bid": _winner_stop_bid,
    })

    while True:
        now_dt = datetime.now(timezone.utc)
        loop_time = asyncio.get_event_loop().time()
        seconds_left = (window_end_dt - now_dt).total_seconds() if window_end_dt else 300.0

        if seconds_left <= es["force_exit_seconds"]:
            # Fix 2: only hold to resolution when EV-positive (mid >= break_even).
            hold_threshold = es.get("hold_for_resolution_mid_threshold", 0.70)
            if es.get("hold_for_resolution_ev_floor", True) and break_even_price:
                hold_threshold = max(hold_threshold, break_even_price)
            # BGGDSB: always hold to $1 resolution — no force exit
            if trade.get("router_bucket") == "bggdsb":
                hold_threshold = 0.0
            mid_check = ws_client.get_mid_price(winner_token)
            if mid_check is not None and mid_check >= hold_threshold:
                _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
                await write_event(trade_id, "held_for_resolution", coin, {
                    "mid": mid_check, "seconds_left": round(seconds_left, 1),
                    "hold_threshold_used": round(hold_threshold, 4),
                })
                await _close_trade(trade_id, 1.0, "held_for_resolution", broadcast_fn)
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

            vel = _vol.get_price_velocity(winner_token) or 0.0
            eff_bid = _estimate_sell_fill(winner_token, size)

            # Fix 1: Hard stop-loss — cap loss magnitude to winner_max_loss_per_trade_eur.
            # Fires when depth-aware fill price would produce > max loss, regardless of score.
            # _winner_stop_bid is None when the feature is disabled (null in config).
            _check_bid = eff_bid if eff_bid is not None else mid
            if _winner_stop_bid is not None and _check_bid <= _winner_stop_bid:
                result = await paper_trader.simulate_market_sell(winner_token, size)
                _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
                await fill_tracker.record_fill_attempt(trade_id, winner_token, "sell", current_limit, False)
                _add_winner_fees(trade_id, result.get("fees", 0.0))
                await write_event(trade_id, "winner_stop_loss_triggered", coin, {
                    "mid": mid, "eff_bid": round(_check_bid, 4),
                    "stop_bid": _winner_stop_bid, "break_even": break_even_price,
                })
                await _close_trade(trade_id, result.get("fill_price"), "winner_stop_loss", broadcast_fn)
                return

            # Fix 3: Velocity stop — exit early on fast reversal while still above the hard stop.
            # Only fires when mid has already dropped meaningfully below our limit target.
            if _vel_stop is not None and vel < _vel_stop and mid < current_limit - 0.05 and phase != "force":
                result = await paper_trader.simulate_market_sell(winner_token, size)
                _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
                await fill_tracker.record_fill_attempt(trade_id, winner_token, "sell", current_limit, False)
                _add_winner_fees(trade_id, result.get("fees", 0.0))
                await write_event(trade_id, "winner_velocity_stop", coin, {
                    "mid": mid, "velocity": round(vel, 5),
                    "eff_bid": round(eff_bid, 4) if eff_bid else None,
                })
                await _close_trade(trade_id, result.get("fill_price"), "winner_velocity_stop", broadcast_fn)
                return

            # Scale-in on confirmed dip recovery: average into the winner across up to
            # _SCALEIN_TRANCHES buys. Each tranche fires only after a ≥6¢ dip from a
            # local high that then bounces ≥2¢ off its low — never a falling knife.
            # TRENDING/NORMAL only; RANGING untouched, CHOPPY/BREAKOUT too erratic.
            _scalein_budget = float(CONFIG["trading"].get("max_scalein_eur", 0.0))
            if _scalein_budget > 0.0:
                _scalein_budget = max(_SCALEIN_MIN_EUR, _scalein_budget)  # enforce €5 minimum
            _tranche_size = round((_scalein_budget / _SCALEIN_TRANCHES) / mid, 2) if mid > 0 else 0
            _sc = _scalein_state.setdefault(trade_id, {"count": 0, "ref_high": mid, "cycle_low": mid})
            if mid > _sc["ref_high"]:            # new local high → start a fresh dip cycle
                _sc["ref_high"] = mid
                _sc["cycle_low"] = mid
            if mid < _sc["cycle_low"]:
                _sc["cycle_low"] = mid
            _dip = _sc["ref_high"] - _sc["cycle_low"]   # depth of the current dip
            _bounce = mid - _sc["cycle_low"]            # recovery off the local low
            if (_scalein_budget > 0.0
                    and trade_regime in ("TRENDING", "NORMAL")
                    and _sc["count"] < _SCALEIN_TRANCHES
                    and break_even_price is not None
                    and _dip >= 0.06                   # genuine dip from local high
                    and _bounce >= 0.02                # recovery confirmed off the low
                    and mid > break_even_price + 0.02  # still profitable to add
                    and phase == "patient"
                    and seconds_left > 120
                    and _tranche_size >= 0.10):
                _sc["count"] += 1
                _sc["ref_high"] = mid                  # next tranche needs a fresh dip-recovery cycle
                _sc["cycle_low"] = mid
                si_result = await paper_trader.simulate_market_buy(winner_token, _tranche_size)
                if si_result.get("filled") and si_result.get("fill_price"):
                    si_price = si_result["fill_price"]
                    si_filled = si_result.get("filled_size", _tranche_size)
                    si_cost = si_price * si_filled + si_result.get("fees", 0.0)
                    new_size = round(size + si_filled, 4)
                    break_even_price = round((break_even_price * size + si_cost) / new_size, 4)
                    size = new_size
                    _winner_stop_bid = round(break_even_price - _max_loss_eur / max(0.01, size), 4)
                    await write_event(trade_id, "scalein_executed", coin, {
                        "mid": round(mid, 4), "si_price": si_price,
                        "si_size": si_filled, "si_cost": round(si_cost, 4),
                        "tranche": _sc["count"], "new_size": size,
                        "new_break_even": break_even_price,
                    })

            # Take it: buy extra winner when market is clearly locking in
            size = await _check_and_execute_take_it(
                trade_id, winner_token, mid, seconds_left,
                size, break_even_price, True, coin,
            )

            score, reason = compute_cross_score(
                mid, peak_mid, current_limit, best_bid, best_ask, seconds_left, es,
                size=size, velocity=vel, effective_bid=eff_bid,
            )

            if score >= params["cross_threshold"]:
                # Don't market-sell a profitable position in patient/urgent phase;
                # let the resting limit fill or force_exit handle it.
                if break_even_price and mid > break_even_price and phase != "force":
                    pass
                elif seconds_left < 90 and mid >= 0.55:
                    pass  # 90s hold zone: prefer limit fill or held_for_resolution over guaranteed loss
                else:
                    result = await paper_trader.simulate_market_sell(winner_token, size)
                    _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
                    await fill_tracker.record_fill_attempt(trade_id, winner_token, "sell", current_limit, False)
                    _add_winner_fees(trade_id, result.get("fees", 0.0))
                    await write_event(trade_id, "peg_cross_triggered", coin, {
                        "score": score, "reason": reason, "phase": phase,
                        "mid": mid, "eff_bid": round(eff_bid, 4) if eff_bid else None,
                        "velocity": round(vel, 5), "seconds_left": round(seconds_left, 1),
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
    bias_cross_adj: float = 0.0,
) -> None:
    """Live mode: peg-cross exit engine — real limit order + dynamic market conversion."""
    trade = get_active_trades().get(trade_id)
    coin = trade["coin"] if trade else "UNKNOWN"
    trade_regime = (trade.get("regime") if trade else None) or "UNKNOWN"
    es = _vol.get_coin_params(coin)
    if bias_cross_adj:
        es["cross_threshold"] = max(0.10, min(0.95, es["cross_threshold"] + bias_cross_adj))
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
    # Pre-compute loss-control constants (stable per trade)
    _max_loss_raw = es.get("winner_max_loss_per_trade_eur", 0.70)
    _max_loss_eur = float(_max_loss_raw) if _max_loss_raw is not None else None  # None = disabled
    _vel_raw = es.get("winner_velocity_stop", -0.004)
    _vel_stop = float(_vel_raw) if _vel_raw is not None else None  # None = disabled (e.g. RANGING)
    _winner_stop_bid = (
        round(break_even_price - _max_loss_eur / max(0.01, size), 4)
        if (break_even_price and _max_loss_eur is not None)
        else None  # None = stop disabled
    )

    limit_resp = await orders.place_limit_order(winner_token, "SELL", current_limit, size)
    current_order_id = limit_resp["order_id"] if limit_resp else None
    if not current_order_id:
        log.error("winner_initial_limit_failed", trade_id=trade_id)
        mkt_resp = await orders.place_market_order(winner_token, "SELL", size)
        _store_trail_metrics(trade_id, peak_mid, 0, 0)
        fill_price = None
        if mkt_resp and mkt_resp.get("order_id"):
            f = await _fetch_market_fill(mkt_resp["order_id"], fallback=peak_mid, size=size)
            fill_price = f["fill_price"]
            _add_winner_fees(trade_id, f["fees"])
        await _close_trade(trade_id, fill_price, "peg_cross", broadcast_fn)
        return

    await write_event(trade_id, "trailing_started", coin, {
        "initial_limit": current_limit, "trigger_price": trigger_price,
        "break_even_price": break_even_price, "winner_stop_bid": _winner_stop_bid,
    })

    while True:
        now_dt = datetime.now(timezone.utc)
        loop_time = asyncio.get_event_loop().time()
        seconds_left = (window_end_dt - now_dt).total_seconds() if window_end_dt else 300.0

        if seconds_left <= es["force_exit_seconds"]:
            # Fix 2: only hold to resolution when EV-positive (mid >= break_even).
            hold_threshold = es.get("hold_for_resolution_mid_threshold", 0.70)
            if es.get("hold_for_resolution_ev_floor", True) and break_even_price:
                hold_threshold = max(hold_threshold, break_even_price)
            # BGGDSB: always hold to $1 resolution
            if trade.get("router_bucket") == "bggdsb":
                hold_threshold = 0.0
            mid_check = ws_client.get_mid_price(winner_token)
            await orders.cancel_order(current_order_id)
            if mid_check is not None and mid_check >= hold_threshold:
                _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
                await write_event(trade_id, "held_for_resolution", coin, {
                    "mid": mid_check, "seconds_left": round(seconds_left, 1),
                    "hold_threshold_used": round(hold_threshold, 4),
                    "note": "winner_tokens_need_redemption_in_polymarket_wallet",
                })
                # Token resolves to exactly $1.00 on-chain; mid at hold time is just confirmation
                await _close_trade(trade_id, 1.0, "held_for_resolution", broadcast_fn)
                return
            mkt_resp = await orders.place_market_order(winner_token, "SELL", size)
            _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
            await fill_tracker.record_fill_attempt(trade_id, winner_token, "sell", current_limit, False)
            fill_price = mid_check
            if mkt_resp and mkt_resp.get("order_id"):
                f = await _fetch_market_fill(mkt_resp["order_id"], fallback=mid_check or 0.5, size=size)
                fill_price = f["fill_price"]
                _add_winner_fees(trade_id, f["fees"])
            else:
                _add_winner_fees(trade_id, paper_trader.taker_fee_rate(mid_check or 0.5) * size)
            await _close_trade(trade_id, fill_price, "force_exit_window_end", broadcast_fn)
            return

        phase = get_exit_phase(seconds_left)
        params = get_phase_params(phase, es)

        mid = ws_client.get_mid_price(winner_token)
        best_bid = ws_client.get_best_bid(winner_token)
        best_ask = ws_client.get_best_ask(winner_token)

        if mid is not None:
            if mid > peak_mid:
                peak_mid = mid

            vel = _vol.get_price_velocity(winner_token) or 0.0
            eff_bid = _estimate_sell_fill(winner_token, size)

            # Fix 1: Hard stop-loss — cap loss magnitude regardless of cross_score or hold-guards.
            # _winner_stop_bid is None when the feature is disabled (null in config).
            _check_bid = eff_bid if eff_bid is not None else mid
            if _winner_stop_bid is not None and _check_bid <= _winner_stop_bid:
                await orders.cancel_order(current_order_id)
                mkt_resp = await orders.place_market_order(winner_token, "SELL", size)
                _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
                await fill_tracker.record_fill_attempt(trade_id, winner_token, "sell", current_limit, False)
                fill_price = mid
                if mkt_resp and mkt_resp.get("order_id"):
                    f = await _fetch_market_fill(mkt_resp["order_id"], fallback=mid or 0.5, size=size)
                    fill_price = f["fill_price"]
                    _add_winner_fees(trade_id, f["fees"])
                else:
                    _add_winner_fees(trade_id, paper_trader.taker_fee_rate(mid or 0.5) * size)
                await write_event(trade_id, "winner_stop_loss_triggered", coin, {
                    "mid": mid, "eff_bid": round(_check_bid, 4),
                    "stop_bid": _winner_stop_bid, "break_even": break_even_price,
                })
                await _close_trade(trade_id, fill_price, "winner_stop_loss", broadcast_fn)
                return

            # Fix 3: Velocity stop — early exit on fast reversal while still above the hard stop.
            if _vel_stop is not None and vel < _vel_stop and mid < current_limit - 0.05 and phase != "force":
                await orders.cancel_order(current_order_id)
                mkt_resp = await orders.place_market_order(winner_token, "SELL", size)
                _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
                await fill_tracker.record_fill_attempt(trade_id, winner_token, "sell", current_limit, False)
                fill_price = mid
                if mkt_resp and mkt_resp.get("order_id"):
                    f = await _fetch_market_fill(mkt_resp["order_id"], fallback=mid or 0.5, size=size)
                    fill_price = f["fill_price"]
                    _add_winner_fees(trade_id, f["fees"])
                else:
                    _add_winner_fees(trade_id, paper_trader.taker_fee_rate(mid or 0.5) * size)
                await write_event(trade_id, "winner_velocity_stop", coin, {
                    "mid": mid, "velocity": round(vel, 5),
                    "eff_bid": round(eff_bid, 4) if eff_bid else None,
                })
                await _close_trade(trade_id, fill_price, "winner_velocity_stop", broadcast_fn)
                return

            # Scale-in on confirmed dip recovery (see _winner_exit_paper for rationale):
            # up to _SCALEIN_TRANCHES buys, each after a ≥6¢ dip that bounces ≥2¢.
            _scalein_budget = float(CONFIG["trading"].get("max_scalein_eur", 0.0))
            if _scalein_budget > 0.0:
                _scalein_budget = max(_SCALEIN_MIN_EUR, _scalein_budget)  # enforce €5 minimum
            _tranche_size = round((_scalein_budget / _SCALEIN_TRANCHES) / mid, 2) if mid > 0 else 0
            _sc = _scalein_state.setdefault(trade_id, {"count": 0, "ref_high": mid, "cycle_low": mid})
            if mid > _sc["ref_high"]:
                _sc["ref_high"] = mid
                _sc["cycle_low"] = mid
            if mid < _sc["cycle_low"]:
                _sc["cycle_low"] = mid
            _dip = _sc["ref_high"] - _sc["cycle_low"]
            _bounce = mid - _sc["cycle_low"]
            if (_scalein_budget > 0.0
                    and trade_regime in ("TRENDING", "NORMAL")
                    and _sc["count"] < _SCALEIN_TRANCHES
                    and break_even_price is not None
                    and _dip >= 0.06
                    and _bounce >= 0.02
                    and mid > break_even_price + 0.02
                    and phase == "patient"
                    and seconds_left > 120
                    and _tranche_size >= 0.10):
                _sc["count"] += 1
                _sc["ref_high"] = mid
                _sc["cycle_low"] = mid
                mkt_buy = await orders.place_market_order(winner_token, "BUY", _tranche_size)
                si_price = mid
                si_fees = 0.0
                if mkt_buy and mkt_buy.get("order_id"):
                    f = await _fetch_market_fill(mkt_buy["order_id"], fallback=mid, size=_tranche_size)
                    si_price = f["fill_price"]
                    si_fees = f["fees"]
                si_cost = si_price * _tranche_size + si_fees
                new_size = round(size + _tranche_size, 4)
                break_even_price = round((break_even_price * size + si_cost) / new_size, 4)
                size = new_size
                _winner_stop_bid = round(break_even_price - _max_loss_eur / max(0.01, size), 4)
                # Reissue limit order for updated total size
                await orders.cancel_order(current_order_id)
                new_resp = await orders.place_limit_order(winner_token, "SELL", current_limit, size)
                if new_resp and new_resp.get("order_id"):
                    current_order_id = new_resp["order_id"]
                await write_event(trade_id, "scalein_executed", coin, {
                    "mid": round(mid, 4), "si_price": si_price,
                    "si_size": _tranche_size, "si_cost": round(si_cost, 4),
                    "tranche": _sc["count"], "new_size": size,
                    "new_break_even": break_even_price,
                })

            # Take it: buy extra winner when market is clearly locking in (live mode)
            prev_size = size
            size = await _check_and_execute_take_it(
                trade_id, winner_token, mid, seconds_left,
                size, break_even_price, False, coin,
            )
            if size != prev_size:
                # Re-issue limit order for updated total size
                await orders.cancel_order(current_order_id)
                new_resp = await orders.place_limit_order(winner_token, "SELL", current_limit, size)
                if new_resp and new_resp.get("order_id"):
                    current_order_id = new_resp["order_id"]

            score, reason = compute_cross_score(
                mid, peak_mid, current_limit, best_bid, best_ask, seconds_left, es,
                size=size, velocity=vel, effective_bid=eff_bid,
            )

            if score >= params["cross_threshold"]:
                if break_even_price and mid > break_even_price and phase != "force":
                    pass
                elif seconds_left < 90 and mid >= 0.55:
                    pass  # 90s hold zone: prefer limit fill or held_for_resolution over guaranteed loss
                else:
                    await orders.cancel_order(current_order_id)
                    mkt_resp = await orders.place_market_order(winner_token, "SELL", size)
                    _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
                    await fill_tracker.record_fill_attempt(trade_id, winner_token, "sell", current_limit, False)
                    fill_price = mid
                    if mkt_resp and mkt_resp.get("order_id"):
                        f = await _fetch_market_fill(mkt_resp["order_id"], fallback=mid or 0.5, size=size)
                        fill_price = f["fill_price"]
                        _add_winner_fees(trade_id, f["fees"])
                    else:
                        _add_winner_fees(trade_id, paper_trader.taker_fee_rate(mid or 0.5) * size)
                    await write_event(trade_id, "peg_cross_triggered", coin, {
                        "score": score, "reason": reason, "phase": phase,
                        "mid": mid, "fill_price": fill_price,
                        "eff_bid": round(eff_bid, 4) if eff_bid else None,
                        "velocity": round(vel, 5), "seconds_left": round(seconds_left, 1),
                    })
                    await _close_trade(trade_id, fill_price, "peg_cross", broadcast_fn)
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
                            mkt_resp = await orders.place_market_order(winner_token, "SELL", size)
                            _store_trail_metrics(trade_id, peak_mid, ratchet_count, loop_time - trail_start)
                            fill_price = mid
                            if mkt_resp and mkt_resp.get("order_id"):
                                f = await _fetch_market_fill(mkt_resp["order_id"], fallback=mid or 0.5, size=size)
                                fill_price = f["fill_price"]
                                _add_winner_fees(trade_id, f["fees"])
                            else:
                                _add_winner_fees(trade_id, paper_trader.taker_fee_rate(mid or 0.5) * size)
                            await _close_trade(trade_id, fill_price, "peg_cross", broadcast_fn)
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

    # Calculate P&L — uses actual per-side sizes (supports weighted bias entries)
    entry_yes = trade.get("entry_yes_price") or ENTRY_PRICE
    entry_no = trade.get("entry_no_price") or ENTRY_PRICE
    base_size = trade.get("entry_size") or 2
    yes_size = float(trade.get("yes_size") or base_size)
    no_size = float(trade.get("no_size") or base_size)
    actual_winner = trade.get("actual_winner") or "YES"
    winner_sz = yes_size if actual_winner == "YES" else no_size
    loser_sz = no_size if actual_winner == "YES" else yes_size
    loser_price = trade.get("loser_exit_price") or 0.30
    winner_price = fill_price or 1.0

    cost = entry_yes * yes_size + entry_no * no_size
    proceeds = loser_price * loser_sz + (winner_price or 0) * winner_sz
    fees = trade.get("fees_paid") or 0.0
    gross_pnl = proceeds - cost
    net_pnl = gross_pnl - fees

    update_trade_field(trade_id, "gross_pnl", round(gross_pnl, 4))
    update_trade_field(trade_id, "net_pnl", round(net_pnl, 4))

    # Adaptive learning: adjust trigger_threshold from real deploy-phase outcomes
    if trade.get("trigger_hit"):
        try:
            from . import adaptive, learning as _learning
            if _learning.get_current_phase() == "deploy":
                adaptive.record_closed_trade(
                    coin=trade["coin"],
                    net_pnl=net_pnl,
                    size=size,
                    loser_exit_price=loser_price,
                    break_even_price=trade.get("break_even_price") or 0.0,
                )
        except Exception:
            pass  # never block trade close on adaptive errors

    # Global consecutive loss tracker
    try:
        from . import risk as _risk
        _risk.record_global_result(net_pnl > 0)
    except Exception:
        pass

    # Coin guard: track result, handle watch/disable state changes
    try:
        from . import coin_guard as _cg
        from .db_sync import get_daily_pnl as _sync_daily_pnl
        _paper = trade.get("mode", "").startswith("paper")
        _daily = _sync_daily_pnl(coin)
        _new_state = await _cg.record_result(coin, net_pnl, _daily, paper=_paper)
        if _new_state == "disabled":
            # Disable in-memory only — do NOT write coin_{coin}_enabled to dashboard_state.
            # The cg_{coin}_state="disabled" persisted by coin_guard is enough to block
            # entries via risk.can_enter(). Writing enabled=False here would also exclude
            # the coin from the scanner on restart, hiding market data unnecessarily.
            CONFIG["coins"][coin]["enabled"] = False
            asyncio.create_task(_cancel_coin_pending_entries(coin, broadcast_fn))
    except Exception:
        pass  # never block trade close on guard errors

    # Oracle: record outcome for track record scoring
    try:
        from . import oracle as _oracle
        _oracle.record_outcome(trade_id, net_pnl > 0)
    except Exception:
        pass

    # BGGDSB streak tracker — skip N windows after breaking a win streak
    try:
        if trade.get("router_bucket") == "bggdsb":
            from .db_sync import get_state as _gs, set_dashboard_state as _sds
            _min_w  = int(CONFIG.get("bggdsb", {}).get("streak_min_wins", 1))
            _skip_n = int(CONFIG.get("bggdsb", {}).get("streak_skip_count", 2))
            _wins   = int(_gs("bggdsb_streak_wins") or 0)
            if net_pnl > 0:
                _sds("bggdsb_streak_wins", str(_wins + 1))
                _sds("bggdsb_streak_skip", "0")
            else:
                if _wins >= _min_w:
                    _sds("bggdsb_streak_skip", str(_skip_n))
                    log.info("bggdsb_streak_broken", streak=_wins, skipping=_skip_n)
                _sds("bggdsb_streak_wins", "0")
    except Exception:
        pass

    # BGGDSB stop-bij-verlies (per munt): pauzeer nieuwe entries voor deze munt
    # na een verlies. Schaduw-trades tellen niet mee — alleen echte/actieve trades.
    try:
        if (trade.get("router_bucket") == "bggdsb"
                and trade.get("triggered_by") != "bggdsb_shadow"
                and net_pnl <= 0):
            from .db_sync import get_state as _gs2, set_dashboard_state as _sds2
            if _gs2("bggdsb_stop_on_loss") == "1":
                _lc = trade.get("coin", "")
                if _lc:
                    _sds2(f"bggdsb_halted_{_lc}", "1")
                    log.info("bggdsb_halted_after_loss", coin=_lc, net_pnl=net_pnl)
    except Exception:
        pass

    await persist_trade(trade_id)
    await write_event(trade_id, "trade_closed", trade["coin"], {
        "reason": reason, "net_pnl": net_pnl, "fill_price": fill_price
    })
    remove_active_trade(trade_id)

    log.info("trade_closed", trade_id=trade_id, reason=reason, net_pnl=net_pnl)
    if broadcast_fn:
        await broadcast_fn({"event": "trade_closed", "trade_id": trade_id, "reason": reason, "net_pnl": net_pnl})


async def _on_trigger_directional(
    trade_id: str, winner: str, broadcast_fn
) -> None:
    """Handle trigger for a directional trade (only one side held).

    Correct direction → trail winner with peg-cross engine.
    Wrong direction   → market-sell our position immediately.
    Resolution        → P&L based on which side expired in-the-money.
    """
    trade = get_active_trades().get(trade_id)
    if not trade:
        return

    directional_side = trade.get("directional_side", "YES")
    coin = trade["coin"]
    yes_token = trade["condition_id_yes"]
    no_token = trade["condition_id_no"]
    our_token = yes_token if directional_side == "YES" else no_token
    our_size = float(trade.get(
        "yes_size" if directional_side == "YES" else "no_size"
    ) or trade.get("entry_size") or 2)
    entry_price = float(trade.get(
        "entry_yes_price" if directional_side == "YES" else "entry_no_price"
    ) or ENTRY_PRICE)
    fees = float(trade.get("fees_paid") or 0.0)

    trade_mode = trade.get("mode") or get_mode()
    if trade_mode == "live_learning":
        from . import learning as _learning
        paper = _learning.get_orchestrator().get_trading_mode().startswith("paper")
    else:
        paper = trade_mode.startswith("paper")

    window_end = trade.get("window_end_ts")
    window_end_dt = datetime.fromisoformat(window_end).astimezone(timezone.utc) if window_end else None
    seconds_to_end = (window_end_dt - datetime.now(timezone.utc)).total_seconds() if window_end_dt else 999.0

    if winner == "RESOLUTION":
        # Window expired — infer outcome from current mid
        our_mid = ws_client.get_mid_price(our_token) or 0.50
        if our_mid >= 0.50:
            # Our side is in the money — hold for $1.00 resolution
            net_pnl = round((1.0 - entry_price) * our_size - fees, 4)
            update_trade_field(trade_id, "winner_exit_reason", "held_for_resolution")
            update_trade_field(trade_id, "winner_exit_price", 1.0)
            update_trade_field(trade_id, "actual_winner", directional_side)
            update_trade_field(trade_id, "status", "resolved")
        else:
            # Our side expired worthless
            net_pnl = round(-entry_price * our_size - fees, 4)
            update_trade_field(trade_id, "winner_exit_reason", "directional_resolution_loss")
            update_trade_field(trade_id, "winner_exit_price", 0.0)
            update_trade_field(trade_id, "actual_winner",
                               "NO" if directional_side == "YES" else "YES")
            update_trade_field(trade_id, "status", "closed")
        update_trade_field(trade_id, "loser_exit_price", 0.0)
        update_trade_field(trade_id, "break_even_price",
                           round(entry_price + fees / max(our_size, 0.01), 4))
        update_trade_field(trade_id, "gross_pnl", round(net_pnl + fees, 4))
        update_trade_field(trade_id, "net_pnl", net_pnl)
        _directional_coin_guard(trade_id, coin, net_pnl, trade, broadcast_fn)
        await persist_trade(trade_id)
        remove_active_trade(trade_id)
        await write_event(trade_id, "directional_resolution", coin,
                          {"net_pnl": net_pnl, "our_mid": our_mid})
        log.info("directional_resolved", trade_id=trade_id, coin=coin, net_pnl=net_pnl)
        if broadcast_fn:
            await broadcast_fn({"event": "trade_resolved", "trade_id": trade_id})
        return

    if winner == directional_side:
        # Correct — trail winner with peg-cross engine
        be_price = round(entry_price + fees / max(our_size, 0.01), 4)
        update_trade_field(trade_id, "break_even_price", be_price)
        update_trade_field(trade_id, "loser_exit_price", 0.0)
        update_trade_field(trade_id, "actual_winner", winner)
        log.info("directional_correct_trigger", trade_id=trade_id, coin=coin,
                 side=directional_side, be_price=be_price)
        await write_event(trade_id, "directional_correct_trigger", coin,
                          {"side": directional_side, "be_price": be_price})
        asyncio.create_task(
            _winner_exit_oco(trade_id, our_token, our_size, paper, broadcast_fn)
        )
        update_trade_field(trade_id, "status", "exiting")
        await persist_trade(trade_id)
        if broadcast_fn:
            await broadcast_fn({"event": "trigger_hit", "trade_id": trade_id, "winner": winner})
    else:
        # Wrong direction — sell what we own at market
        log.warning("directional_wrong_trigger", trade_id=trade_id, coin=coin,
                    predicted=directional_side, actual_winner=winner)
        loser_result = await _sell_loser_with_limit(
            our_token, our_size, paper, coin, trade_id, seconds_left=seconds_to_end
        )
        loser_price = loser_result.get("fill_price") or 0.17
        loser_fees = loser_result.get("fees") or 0.0
        update_trade_field(trade_id, "actual_winner", winner)
        update_trade_field(trade_id, "loser_exit_price", loser_price)
        update_trade_field(trade_id, "fees_paid", fees + loser_fees)
        await write_event(trade_id, "directional_wrong_trigger", coin,
                          {"predicted": directional_side, "actual_winner": winner,
                           "sell_price": loser_price})
        # winner_exit_price = 0.0 — we don't own the winner side
        # _close_trade handles P&L: proceeds = loser_price * our_size + 0 * 0
        await _close_trade(trade_id, 0.0, "directional_wrong_side", broadcast_fn)


def _directional_coin_guard(trade_id, coin, net_pnl, trade, broadcast_fn) -> None:
    """Fire-and-forget coin guard update for directional resolution (sync wrapper)."""
    try:
        from . import coin_guard as _cg
        from .db_sync import get_daily_pnl as _sync_daily_pnl
        from .logger import save_dashboard_state as _save_state
        _paper = trade.get("mode", "").startswith("paper")
        _daily = _sync_daily_pnl(coin)
        asyncio.create_task(_run_directional_guard(coin, net_pnl, _daily, _paper, broadcast_fn))
    except Exception:
        pass


async def _run_directional_guard(coin, net_pnl, daily_pnl, paper, broadcast_fn) -> None:
    try:
        from . import risk as _risk
        _risk.record_global_result(net_pnl > 0)
    except Exception:
        pass
    try:
        from . import coin_guard as _cg
        _new_state = await _cg.record_result(coin, net_pnl, daily_pnl, paper=paper)
        if _new_state == "disabled":
            # In-memory disable only — see comment in close_straddle_trade for rationale.
            CONFIG["coins"][coin]["enabled"] = False
            asyncio.create_task(_cancel_coin_pending_entries(coin, broadcast_fn))
    except Exception:
        pass


async def _cancel_coin_pending_entries(coin: str, broadcast_fn=None) -> None:
    """Cancel pending (not-yet-filled) entry orders for a coin when the guard disables it."""
    from .state import get_active_trades, persist_trade as _persist, remove_active_trade
    trades = get_active_trades()
    for tid, t in list(trades.items()):
        if t.get("coin") != coin or t.get("status") != "pending":
            continue
        for oid_field in ("yes_order_id", "no_order_id"):
            oid = t.get(oid_field)
            if oid:
                try:
                    await orders.cancel_order(oid)
                except Exception:
                    pass
        update_trade_field(tid, "status", "aborted")
        update_trade_field(tid, "notes", "cancelled: coin_disabled_by_guard")
        await _persist(tid)
        remove_active_trade(tid)
        log.warning("pending_entry_cancelled_by_guard", trade_id=tid, coin=coin)
        if broadcast_fn:
            await broadcast_fn({"event": "coin_guard_disabled", "coin": coin, "trade_id": tid})


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

    # Stamp actual_winner from current mid for Signal Lab accuracy tracking.
    # Straddle always earns $1 regardless, but knowing which side won lets us
    # evaluate whether conviction_at_entry predicted the correct direction.
    try:
        coin = trade.get("coin", "")
        yes_token = trade.get("condition_id_yes")
        if coin and yes_token:
            from . import ws_client as _ws
            mid = _ws.get_mid(yes_token)
            if mid is not None:
                update_trade_field(trade_id, "actual_winner", "YES" if mid >= 0.5 else "NO")
    except Exception:
        pass

    # Coin guard: resolutions count as wins (full $1.00 payout)
    try:
        from . import coin_guard as _cg
        from .db_sync import get_daily_pnl as _sync_daily_pnl
        from . import risk as _risk
        _paper = trade.get("mode", "").startswith("paper")
        coin = trade.get("coin", "")
        if coin:
            _risk.record_global_result(net_pnl > 0)
            _daily = _sync_daily_pnl(coin)
            _new_state = await _cg.record_result(coin, net_pnl, _daily, paper=_paper)
            if _new_state == "disabled":
                # In-memory disable only — see comment in close_straddle_trade for rationale.
                CONFIG["coins"][coin]["enabled"] = False
                asyncio.create_task(_cancel_coin_pending_entries(coin, broadcast_fn))
    except Exception:
        pass

    # Oracle: record outcome for track record scoring
    try:
        from . import oracle as _oracle
        _oracle.record_outcome(trade_id, net_pnl > 0)
    except Exception:
        pass

    await persist_trade(trade_id)
    remove_active_trade(trade_id)
    await write_event(trade_id, "resolution", trade["coin"], {"net_pnl": net_pnl})
    log.info("trade_resolved", trade_id=trade_id, net_pnl=net_pnl)
    if broadcast_fn:
        await broadcast_fn({"event": "trade_resolved", "trade_id": trade_id})
