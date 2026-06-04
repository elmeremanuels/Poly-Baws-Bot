"""In-memory bot state + persistence/recovery from SQLite."""
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from .logger import log, get_open_trades, write_trade, update_trade, write_event
from .config_loader import CONFIG

# In-memory state
_active_trades: dict[str, dict] = {}   # trade_id -> trade state
_window_registry: set[tuple[str, str]] = set()  # (coin, window_start_ts) already traded

_mode = "paper_hybrid"   # paper_hybrid / paper_auto / live_hybrid / live_auto / live_learning / signal_trader / auto_router


def get_mode() -> str:
    return _mode


def set_mode(mode: str) -> None:
    global _mode
    valid = {"paper_hybrid", "paper_auto", "live_hybrid", "live_auto", "live_learning", "signal_trader", "auto_router", "bggdsb_paper", "bggdsb_live", "stoplicht_scalper"}
    if mode not in valid:
        raise ValueError(f"Invalid mode: {mode}. Must be one of {valid}")
    _mode = mode
    log.info("mode_changed", mode=mode)


def is_paper_mode() -> bool:
    # signal_trader has its own paper_mode flag in config
    return _mode.startswith("paper") or _mode.endswith("_paper")


def is_auto_mode() -> bool:
    return _mode.endswith("auto")


def get_active_trades() -> dict[str, dict]:
    return dict(_active_trades)


def get_active_count_by_coin() -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in _active_trades.values():
        coin = t.get("coin", "UNKNOWN")
        counts[coin] = counts.get(coin, 0) + 1
    return counts


def has_traded_window(coin: str, window_start_ts: str) -> bool:
    return (coin, window_start_ts) in _window_registry


def register_window_trade(coin: str, window_start_ts: str) -> None:
    _window_registry.add((coin, window_start_ts))


def create_trade_state(
    coin: str,
    market: dict,
    mode: str,
    triggered_by: str = "bot",
) -> dict:
    # Use pre-generated ID from Oracle verdict linkage if provided
    trade_id = market.get("_pre_trade_id") or str(uuid.uuid4())
    window_start = market["window_start"]
    window_end = market["window_end"]

    state = {
        "trade_id": trade_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "coin": coin,
        "market_id": market.get("market_id"),
        "condition_id_yes": market.get("yes_token"),
        "condition_id_no": market.get("no_token"),
        "question": market.get("question", ""),   # market title — zoekbaar op Polymarket
        "mode": mode,
        "triggered_by": triggered_by,
        "window_start_ts": window_start.isoformat() if window_start else None,
        "window_end_ts": window_end.isoformat() if window_end else None,
        "status": "pending",
        "entry_placed_ts": None,
        "entry_filled_ts": None,
        "entry_yes_price": None,
        "entry_no_price": None,
        "entry_size": CONFIG["trading"]["entry_size_shares"],
        "yes_order_id": None,
        "no_order_id": None,
        "trigger_hit": False,
        "trigger_ts": None,
        "winner_side": None,
        "loser_exit_price": None,
        "loser_exit_ts": None,
        "winner_exit_price": None,
        "winner_exit_ts": None,
        "winner_exit_reason": None,
        "fees_paid": 0.0,
        "gross_pnl": None,
        "net_pnl": None,
        "notes": "",
        "peak_bid": None,
        "ratchet_count": 0,
        "time_in_trail_seconds": None,
        "mid_at_trigger": None,
        "spread_at_trigger": None,
        "mid_velocity_at_trigger": None,
        "yes_depth_at_trigger": None,
        "no_depth_at_trigger": None,
        "time_since_window_start": None,
        "phase": "manual",
        "cycle_id": None,
        "param_snapshot": None,
        "break_even_price": None,
        "actual_winner": None,
        "bias_certainty": None,
        "yes_size": None,
        "no_size": None,
        "regime_at_entry": None,
        "bias_direction_at_entry": None,
        # Phase 1 signals stamped at entry
        "ofi_at_entry": None,
        "funding_rate_at_entry": None,
        "liq_proxy_at_entry": None,
        "conviction_at_entry": None,
        "conviction_score_at_entry": None,
        # Phase 1 signals stamped at trigger
        "ofi_at_trigger": None,
        "funding_rate_at_trigger": None,
        "liq_proxy_at_trigger": None,
        "conviction_at_trigger": None,
        "conviction_score_at_trigger": None,
        # Auto Router routing decision
        "router_bucket": market.get("_router_bucket"),
        "router_conviction_score": market.get("_router_conviction_score"),
        # Early loser sell (monitoring phase)
        "early_loser_side": None,       # "YES"/"NO" once early sell fills
        "early_loser_price": None,      # fill price of the early sell
        "early_loser_ts": None,
        "early_loser_rebought": False,  # True if re-bought after wrong-side early sell
        # BGGDSB sizing — set before execute_entry to override conviction weighting
        "bggdsb_yes_shares": market.get("_bggdsb_yes_shares"),
        "bggdsb_no_shares": market.get("_bggdsb_no_shares"),
        # BGGDSB hedge tracking
        "bggdsb_dominant_side": market.get("_bggdsb_dominant_side"),
        "bggdsb_hedge_placed": False,
    }
    return state


def add_active_trade(state: dict, skip_window_register: bool = False) -> None:
    _active_trades[state["trade_id"]] = state
    if skip_window_register:
        return
    win = state.get("window_start_ts", "")
    if win:
        register_window_trade(state["coin"], win)


def remove_active_trade(trade_id: str) -> None:
    _active_trades.pop(trade_id, None)


def reset_state() -> None:
    """Wipe all in-memory trade state (used for paper trading reset)."""
    _active_trades.clear()
    _window_registry.clear()
    log.info("in_memory_state_reset")


def update_trade_field(trade_id: str, field: str, value: Any) -> None:
    if trade_id in _active_trades:
        _active_trades[trade_id][field] = value


async def persist_trade(trade_id: str) -> None:
    if trade_id not in _active_trades:
        return
    state = _active_trades[trade_id]
    # Filter to DB columns only (exclude runtime-only fields)
    db_fields = {
        "trade_id", "created_at",
        "coin", "market_id", "condition_id_yes", "condition_id_no",
        "question",
        "mode", "triggered_by", "window_start_ts", "window_end_ts",
        "entry_placed_ts", "entry_filled_ts", "entry_yes_price", "entry_no_price",
        "entry_size", "yes_size", "no_size", "bias_certainty",
        "trigger_hit", "trigger_ts", "winner_side",
        "loser_exit_price", "loser_exit_ts", "winner_exit_price", "winner_exit_ts",
        "winner_exit_reason", "fees_paid", "gross_pnl", "net_pnl", "status", "notes",
        "peak_bid", "ratchet_count", "time_in_trail_seconds",
        "mid_at_trigger", "spread_at_trigger", "mid_velocity_at_trigger",
        "yes_depth_at_trigger", "no_depth_at_trigger", "time_since_window_start",
        "phase", "cycle_id", "param_snapshot",
        "break_even_price", "actual_winner",
        "regime_at_entry", "bias_direction_at_entry",
        "ofi_at_entry", "funding_rate_at_entry", "liq_proxy_at_entry",
        "conviction_at_entry", "conviction_score_at_entry",
        "ofi_at_trigger", "funding_rate_at_trigger", "liq_proxy_at_trigger",
        "conviction_at_trigger", "conviction_score_at_trigger",
        "early_loser_side", "early_loser_price", "early_loser_ts", "early_loser_rebought",
        "router_bucket", "router_conviction_score",
    }
    record = {k: v for k, v in state.items() if k in db_fields}
    await write_trade(record)


async def recover_state() -> None:
    """On startup, reload open trades from DB into memory."""
    open_trades = await get_open_trades()
    for trade in open_trades:
        _active_trades[trade["trade_id"]] = trade
        win = trade.get("window_start_ts", "")
        if win:
            register_window_trade(trade["coin"], win)
    log.info("state_recovered", open_trades=len(open_trades))
    return open_trades
