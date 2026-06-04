"""Stoplicht Scalper — 15-minute directional scalper for Polymarket.

Entry rule: stoplicht is GROEN (combined score ≥ 0.60) at T-2min before window.
Phase 1 (> phase2_secs remaining): trailing stop + MOM reversal gate.
Phase 2 (≤ phase2_secs remaining): if winning-side mid ≥ hold_threshold → hold to $1.

Paper mode: fills simulated via ws_client bid/ask prices.
Self-learning hold threshold: recalibrated from window_tradelog resolution rate.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import json

from .config_loader import CONFIG
from .logger import log, write_window_tradelog, save_dashboard_state
from . import scanner, ws_client, risk
from .state import has_traded_window, register_window_trade, get_mode
from .stoplicht_signals import get_stoplicht, _compute_momentum

# Per-coin self-learning hold threshold
_hold_thresholds: dict[str, float] = {}


async def _save_stoplicht_state(coin: str, color: str, direction: str | None, score: float) -> None:
    """Persist stoplicht state to dashboard_state so Streamlit can read it."""
    try:
        from datetime import datetime, timezone as _tz
        payload = json.dumps({
            "color": color,
            "direction": direction,
            "score": score,
            "updated_at": datetime.now(_tz.utc).isoformat(),
        })
        await save_dashboard_state(f"scalper_stoplicht_{coin}", payload)
    except Exception:
        pass


def _get_hold_threshold(coin: str) -> float:
    cfg = CONFIG.get("stoplicht_scalper", {})
    return _hold_thresholds.get(coin, cfg.get("hold_threshold_init", 0.88))


def _update_hold_threshold(coin: str, value: float) -> None:
    _hold_thresholds[coin] = max(0.75, min(0.95, value))


def _paper_entry_price(direction: str, market: dict) -> float | None:
    """Simulate entry fill: ask price of the direction token + slippage."""
    token = market.get("yes_token") if direction == "YES" else market.get("no_token")
    if not token:
        return None
    slippage = CONFIG.get("fees", {}).get("paper_slippage_per_share", 0.005)
    ask = ws_client.get_best_ask(token)
    if ask and 0.01 < ask < 0.99:
        return min(0.99, ask + slippage)
    mid = ws_client.get_mid_price(token)
    return mid if mid and 0.01 < mid < 0.99 else None


def _get_direction_mid(direction: str, market: dict) -> float | None:
    """Current mid price of the directional token."""
    token = market.get("yes_token") if direction == "YES" else market.get("no_token")
    return ws_client.get_mid_price(token) if token else None


def _paper_exit_price(direction: str, market: dict) -> float | None:
    """Simulate exit fill: bid price of the direction token."""
    token = market.get("yes_token") if direction == "YES" else market.get("no_token")
    if not token:
        return None
    bid = ws_client.get_best_bid(token)
    if bid and 0.01 < bid < 0.99:
        return bid
    mid = ws_client.get_mid_price(token)
    return mid if mid and 0.01 < mid < 0.99 else None


def _estimate_net_pnl(entry_price: float, exit_price: float, trade_size_eur: float) -> float:
    """Net P&L after estimated Polymarket taker fees."""
    if entry_price <= 0:
        return 0.0
    shares = trade_size_eur / entry_price
    gross = (exit_price - entry_price) * shares
    fee_entry = 0.018 * min(entry_price, 1 - entry_price) / 0.5 * trade_size_eur
    fee_exit = 0.018 * min(exit_price, 1 - exit_price) / 0.5 * (shares * exit_price) if exit_price < 1.0 else 0.0
    return round(gross - fee_entry - fee_exit, 4)


async def _recalibrate_hold_threshold(coin: str) -> None:
    """Adjust hold threshold based on recent held-to-end outcomes."""
    from .logger import _db
    try:
        async with _db() as db:
            async with db.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN pnl_eur > 0 THEN 1 ELSE 0 END) AS wins
                FROM window_tradelog
                WHERE coin = ? AND exit_reason = 'held_to_end'
                  AND created_at >= datetime('now', '-7 days')
            """, (coin,)) as cur:
                row = await cur.fetchone()
        if not row or (row[0] or 0) < 10:
            return
        win_rate = (row[1] or 0) / row[0]
        current = _get_hold_threshold(coin)
        if win_rate >= 0.80 and current > 0.83:
            _update_hold_threshold(coin, current - 0.02)
            log.info("scalper_threshold_lowered", coin=coin, new=_get_hold_threshold(coin), win_rate=win_rate)
        elif win_rate < 0.60 and current < 0.93:
            _update_hold_threshold(coin, current + 0.02)
            log.info("scalper_threshold_raised", coin=coin, new=_get_hold_threshold(coin), win_rate=win_rate)
    except Exception as exc:
        log.debug("scalper_recalibrate_error", coin=coin, error=str(exc))


async def _run_window(
    coin: str,
    market: dict,
    stoplicht_color: str,
    direction: str,
    paper: bool,
) -> None:
    """Trade one window: enter at start, trail/hold until exit or window end."""
    cfg = CONFIG.get("stoplicht_scalper", {})
    trade_size_eur = cfg.get("trade_size_eur", 1.00)
    phase2_secs = cfg.get("phase2_secs", 180)
    trail_activate = cfg.get("trail_activate_cts", 5) / 100.0
    trail_buffer = cfg.get("trail_buffer_cts", 2) / 100.0
    hold_threshold = _get_hold_threshold(coin)

    window_start = market["window_start"]
    window_end = market["window_end"]
    window_id = f"{coin}-{int(window_start.timestamp())}"

    # Wait for window start
    now = datetime.now(timezone.utc)
    wait_secs = (window_start - now).total_seconds()
    if wait_secs > 0:
        await asyncio.sleep(wait_secs)

    # Confirm stoplicht is still valid right before entering
    try:
        confirm_color, confirm_dir, _ = await get_stoplicht(coin)
        if confirm_color == "ROOD" or confirm_dir != direction:
            log.info("scalper_entry_cancelled", coin=coin, reason="stoplicht_changed",
                     was=direction, now=confirm_dir, color=confirm_color)
            await write_window_tradelog({
                "window_id": window_id, "coin": coin,
                "window_start": window_start.isoformat(), "window_end": window_end.isoformat(),
                "stoplicht": stoplicht_color, "direction": direction,
                "exit_reason": "stoplicht_changed", "pnl_eur": 0.0,
                "hold_threshold_used": hold_threshold, "paper": 1 if paper else 0,
            })
            return
    except Exception:
        pass

    # Get entry price
    entry_price = _paper_entry_price(direction, market)
    if entry_price is None:
        log.warning("scalper_no_entry_price", coin=coin, window_id=window_id)
        await write_window_tradelog({
            "window_id": window_id, "coin": coin,
            "window_start": window_start.isoformat(), "window_end": window_end.isoformat(),
            "stoplicht": stoplicht_color, "direction": direction,
            "exit_reason": "no_price_data", "pnl_eur": 0.0,
            "hold_threshold_used": hold_threshold, "paper": 1 if paper else 0,
        })
        return

    log.info("scalper_entry", coin=coin, direction=direction, entry_price=entry_price,
             stoplicht=stoplicht_color, paper=paper, window_id=window_id)

    peak_price = entry_price
    trailing_active = False
    exit_price: float | None = None
    exit_reason: str | None = None

    while True:
        now = datetime.now(timezone.utc)
        secs_remaining = (window_end - now).total_seconds()

        if secs_remaining <= 0:
            # Window expired — use last known price
            exit_price = _paper_exit_price(direction, market) or entry_price
            exit_reason = "force_exit"
            break

        current_mid = _get_direction_mid(direction, market)
        if current_mid is None:
            await asyncio.sleep(0.5)
            continue

        if current_mid > peak_price:
            peak_price = current_mid

        if not trailing_active and peak_price >= entry_price + trail_activate:
            trailing_active = True
            log.debug("scalper_trail_activated", coin=coin, peak=peak_price, entry=entry_price)

        if secs_remaining <= phase2_secs:
            # Phase 2: hold to resolution if strong signal
            if current_mid >= hold_threshold:
                exit_price = 1.00  # assume $1 resolution
                exit_reason = "held_to_end"
                log.info("scalper_hold_to_end", coin=coin, mid=current_mid,
                         threshold=hold_threshold, secs_remaining=round(secs_remaining, 0))
                break
            # Phase 2 trailing stop
            if trailing_active and current_mid <= peak_price - trail_buffer:
                exit_price = _paper_exit_price(direction, market) or current_mid
                exit_reason = "trail_stop"
                log.info("scalper_trail_stop_p2", coin=coin, current=current_mid, peak=peak_price)
                break
        else:
            # Phase 1: MOM reversal gate
            try:
                _, new_dir, new_score = await get_stoplicht(coin)
                if new_dir is not None and new_dir != direction and new_score >= 0.40:
                    exit_price = _paper_exit_price(direction, market) or current_mid
                    exit_reason = "mom_reversal"
                    log.info("scalper_mom_reversal", coin=coin, was=direction, new=new_dir)
                    break
            except Exception:
                pass

            # Phase 1 trailing stop
            if trailing_active and current_mid <= peak_price - trail_buffer:
                exit_price = _paper_exit_price(direction, market) or current_mid
                exit_reason = "trail_stop"
                log.info("scalper_trail_stop_p1", coin=coin, current=current_mid, peak=peak_price)
                break

        await asyncio.sleep(1.5)

    # Calculate net P&L
    if exit_price is not None:
        net_pnl = _estimate_net_pnl(entry_price, exit_price, trade_size_eur)
    else:
        net_pnl = 0.0

    log.info("scalper_closed", coin=coin, direction=direction,
             entry=entry_price, exit=exit_price, reason=exit_reason,
             pnl=net_pnl, paper=paper)

    await write_window_tradelog({
        "window_id": window_id, "coin": coin,
        "window_start": window_start.isoformat(), "window_end": window_end.isoformat(),
        "stoplicht": stoplicht_color, "direction": direction,
        "entry_price": entry_price, "exit_price": exit_price,
        "exit_reason": exit_reason, "pnl_eur": net_pnl,
        "hold_threshold_used": hold_threshold, "paper": 1 if paper else 0,
    })

    await _recalibrate_hold_threshold(coin)


async def scalper_loop(coin: str) -> None:
    """Per-coin main loop for stoplicht_scalper mode.

    Self-gates on mode: sleeps when not in stoplicht_scalper mode.
    Evaluates stoplicht in the entry window, commits at T-commit_secs.
    """
    cfg = CONFIG.get("stoplicht_scalper", {})
    scalper_coin = cfg.get("coin", "BTC")
    market_filter = cfg.get("market_filter", "btc-updown-15m")
    paper = cfg.get("paper_mode", True)
    entry_start_secs = cfg.get("entry_start_secs", 600)
    commit_secs = cfg.get("commit_secs", 120)
    entry_cutoff_secs = cfg.get("entry_cutoff_secs", 30)
    poll_secs = cfg.get("poll_interval_secs", 10)

    # Only run for the configured coin
    if coin != scalper_coin:
        return

    log.info("scalper_loop_started", coin=coin, filter=market_filter, paper=paper)

    while True:
        try:
            if get_mode() != "stoplicht_scalper":
                await asyncio.sleep(5)
                continue

            if risk.is_killed():
                await asyncio.sleep(5)
                continue

            market = await scanner.get_next_scalper_market(coin, market_filter)
            if not market or not market.get("window_start"):
                await asyncio.sleep(poll_secs)
                continue

            window_start = market["window_start"]
            window_ts = window_start.isoformat()

            if has_traded_window(coin, window_ts):
                await asyncio.sleep(poll_secs)
                continue

            now = datetime.now(timezone.utc)
            secs_to_start = (window_start - now).total_seconds()

            # Outside evaluation window
            if secs_to_start > entry_start_secs:
                await asyncio.sleep(poll_secs)
                continue

            # Too late to enter
            if secs_to_start < entry_cutoff_secs:
                register_window_trade(coin, window_ts)
                await asyncio.sleep(poll_secs)
                continue

            # Pre-evaluation window (entry_start → commit_secs): log but don't commit
            if secs_to_start > commit_secs:
                try:
                    color, direction, score = await get_stoplicht(coin)
                    log.debug("scalper_pre_eval", coin=coin, color=color,
                              score=score, secs=round(secs_to_start, 0))
                    await _save_stoplicht_state(coin, color, direction, score)
                except Exception:
                    pass
                await asyncio.sleep(poll_secs)
                continue

            # Commit window: T-commit_secs or closer — make final decision
            color, direction, score = await get_stoplicht(coin)
            await _save_stoplicht_state(coin, color, direction, score)
            log.info("scalper_commit_eval", coin=coin, color=color, direction=direction,
                     score=score, secs_to_start=round(secs_to_start, 0))

            # Claim the window regardless of result (prevents double-evaluation)
            register_window_trade(coin, window_ts)

            if color == "GROEN" and direction is not None:
                await _run_window(coin, market, color, direction, paper)
            else:
                window_end = market.get("window_end")
                await write_window_tradelog({
                    "window_id": f"{coin}-{int(window_start.timestamp())}",
                    "coin": coin,
                    "window_start": window_ts,
                    "window_end": window_end.isoformat() if window_end else None,
                    "stoplicht": color, "direction": direction,
                    "exit_reason": "no_entry_signal", "pnl_eur": 0.0,
                    "hold_threshold_used": _get_hold_threshold(coin),
                    "paper": 1 if paper else 0,
                })

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("scalper_loop_error", coin=coin, error=str(exc))

        await asyncio.sleep(poll_secs)
