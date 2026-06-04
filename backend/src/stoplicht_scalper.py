"""Stoplicht Scalper — 15-minute directional scalper for Polymarket.

Phase 1 (>phase2_secs remaining): active scalping on stoplicht consensus.
  Multiple round-trips per window: enter on consensus+confirmed,
  exit on trailing stop or MOM reversal, re-enter when signal re-aligns.
  Optional contrarian hedge when support/wall detected.
Phase 2 (≤phase2_secs remaining): anticipation mode.
  winning_mid ≥ hold_threshold → hold to $1 resolution.
  Otherwise → take final directional position on consensus.

Paper mode: fills simulated via ws_client bid/ask.
Self-learning hold threshold via window_tradelog outcomes.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json

from .config_loader import CONFIG
from .logger import log, write_window_tradelog, save_dashboard_state
from . import scanner, ws_client, risk
from .state import has_traded_window, register_window_trade, get_mode
from .stoplicht_signals import get_stoplicht, get_stoplicht_dict
from .position_manager import WindowPositions, get_position_sizes
from .distance_proxy import get_winning_side

_hold_thresholds: dict[str, float] = {}


async def _save_stoplicht_state(coin: str, color: str, direction: str | None, score: float) -> None:
    try:
        payload = json.dumps({
            "color": color,
            "direction": direction,
            "score": score,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        await save_dashboard_state(f"scalper_stoplicht_{coin}", payload)
    except Exception:
        pass


def _get_hold_threshold(coin: str) -> float:
    cfg = CONFIG.get("stoplicht_scalper", {})
    return _hold_thresholds.get(coin, cfg.get("hold_threshold_init", 0.88))


def _update_hold_threshold(coin: str, value: float) -> None:
    _hold_thresholds[coin] = max(0.75, min(0.95, value))


def _paper_entry_price(token_direction: str, market: dict) -> float | None:
    token = market.get("yes_token") if token_direction == "YES" else market.get("no_token")
    if not token:
        return None
    slippage = CONFIG.get("fees", {}).get("paper_slippage_per_share", 0.005)
    ask = ws_client.get_best_ask(token)
    if ask and 0.01 < ask < 0.99:
        return min(0.99, ask + slippage)
    mid = ws_client.get_mid_price(token)
    return mid if mid and 0.01 < mid < 0.99 else None


def _paper_exit_price(token_direction: str, market: dict) -> float | None:
    token = market.get("yes_token") if token_direction == "YES" else market.get("no_token")
    if not token:
        return None
    bid = ws_client.get_best_bid(token)
    if bid and 0.01 < bid < 0.99:
        return bid
    mid = ws_client.get_mid_price(token)
    return mid if mid and 0.01 < mid < 0.99 else None


def _get_token_mid(token_direction: str, market: dict) -> float | None:
    token = market.get("yes_token") if token_direction == "YES" else market.get("no_token")
    return ws_client.get_mid_price(token) if token else None


def _tick_trailing(pos, market, trail_activate: float, trail_buffer: float):
    """Update trailing state. Returns (should_exit: bool, current_mid: float|None)."""
    current_mid = _get_token_mid(pos.token_direction, market)
    if current_mid is None:
        return False, None
    if current_mid > pos.peak_price:
        pos.peak_price = current_mid
    if not pos.trailing_active and pos.peak_price >= pos.entry_price + trail_activate:
        pos.trailing_active = True
    should_exit = pos.trailing_active and current_mid <= pos.peak_price - trail_buffer
    return should_exit, current_mid


async def _recalibrate_hold_threshold(coin: str) -> None:
    from .logger import _db
    try:
        async with _db() as db:
            async with db.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN pnl_eur > 0 THEN 1 ELSE 0 END) AS wins
                FROM window_tradelog
                WHERE coin = ? AND winning_mid_at_phase2 IS NOT NULL
                  AND winning_mid_at_phase2 >= ?
                  AND created_at >= datetime('now', '-7 days')
            """, (coin, _get_hold_threshold(coin) - 0.05)) as cur:
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


async def _phase1_tick(coin, market, positions, st, lead, secs_left,
                       trail_activate, trail_buffer, hedge_enabled, hold_threshold, phase2_secs):
    """Phase 1 tick: manage open positions + try new entries on full consensus."""
    sizes = get_position_sizes()

    # ── Manage main position ───────────────────────────────────────────────────
    if positions.main:
        pos = positions.main
        should_exit, current_mid = _tick_trailing(pos, market, trail_activate, trail_buffer)

        # MOM reversal: stoplicht flipped to opposite direction with conviction
        new_dir = st.get("direction")
        if new_dir and new_dir != pos.direction and st.get("score", 0) >= 0.40:
            exit_p = _paper_exit_price(pos.token_direction, market) or current_mid or pos.entry_price
            pnl = positions.close_main(exit_p, "mom_reversal")
            log.info("scalper_mom_exit", coin=coin, pnl=pnl, secs_left=round(secs_left))
            return

        if should_exit and current_mid:
            exit_p = _paper_exit_price(pos.token_direction, market) or current_mid
            pnl = positions.close_main(exit_p, "trail_stop")
            log.info("scalper_trail_exit", coin=coin, pnl=pnl, secs_left=round(secs_left))
            return

    # ── Manage hedge position ──────────────────────────────────────────────────
    if positions.hedge:
        pos = positions.hedge
        should_exit, current_mid = _tick_trailing(pos, market, trail_activate, trail_buffer)
        if should_exit and current_mid:
            exit_p = _paper_exit_price(pos.token_direction, market) or current_mid
            positions.close_hedge(exit_p, "trail_stop")

    # ── Open main position: full consensus + market confirmation ───────────────
    if positions.can_open_main() and st.get("consensus") and st.get("confirmed"):
        direction = st.get("direction")
        if direction:
            token_dir = "YES" if direction == "UP" else "NO"
            entry_p = _paper_entry_price(token_dir, market)
            if entry_p:
                positions.open_main(direction, entry_p, sizes["main_eur"])
                log.info("scalper_entry", coin=coin, direction=direction,
                         entry=entry_p, size=sizes["main_eur"], secs_left=round(secs_left))

    # ── Contrarian hedge: support wall in sight, enough time, market beweeglijk ─
    if (hedge_enabled
            and positions.can_open_hedge()
            and st.get("support_near")
            and secs_left > phase2_secs + 60
            and lead and lead["winning_mid"] < hold_threshold):
        bounce_dir = st.get("support_bounce_direction")
        if bounce_dir:
            token_dir = "YES" if bounce_dir == "UP" else "NO"
            entry_p = _paper_entry_price(token_dir, market)
            if entry_p:
                positions.open_hedge(bounce_dir, entry_p, sizes["hedge_eur"])
                log.info("scalper_hedge_entry", coin=coin, direction=bounce_dir, entry=entry_p)


async def _phase2_tick(coin, market, positions, st, lead, secs_left,
                       trail_activate, trail_buffer, hold_threshold):
    """Phase 2 tick: hold if winning side strong, else take final position."""
    sizes = get_position_sizes()

    if lead and lead["winning_mid"] >= hold_threshold:
        # Hold mode: trailing still active, no MOM exits, no new entries
        for slot in ("main", "hedge"):
            pos = getattr(positions, slot)
            if pos:
                should_exit, current_mid = _tick_trailing(pos, market, trail_activate, trail_buffer)
                if should_exit and current_mid:
                    exit_p = _paper_exit_price(pos.token_direction, market) or current_mid
                    if slot == "main":
                        positions.close_main(exit_p, "trail_stop_p2")
                    else:
                        positions.close_hedge(exit_p, "trail_stop_p2")
        return

    # No hold signal — take final directional position if consensus, no current position
    if positions.can_open_main() and st.get("consensus"):
        direction = st.get("direction")
        if direction:
            token_dir = "YES" if direction == "UP" else "NO"
            entry_p = _paper_entry_price(token_dir, market)
            if entry_p:
                positions.open_main(direction, entry_p, sizes["main_eur"])
                log.info("scalper_phase2_entry", coin=coin, direction=direction,
                         entry=entry_p, secs_left=round(secs_left))

    # Continue trailing existing positions
    for slot in ("main", "hedge"):
        pos = getattr(positions, slot)
        if pos:
            should_exit, current_mid = _tick_trailing(pos, market, trail_activate, trail_buffer)
            if should_exit and current_mid:
                exit_p = _paper_exit_price(pos.token_direction, market) or current_mid
                if slot == "main":
                    positions.close_main(exit_p, "trail_stop_p2")
                else:
                    positions.close_hedge(exit_p, "trail_stop_p2")


async def _run_window(coin: str, market: dict, paper: bool) -> None:
    """Full 15-minute window: Phase 1 active scalping + Phase 2 anticipation."""
    cfg = CONFIG.get("stoplicht_scalper", {})
    phase2_secs = cfg.get("phase2_secs", 180)
    hold_threshold = _get_hold_threshold(coin)
    trail_activate = cfg.get("trail_activate_cts", 5) / 100.0
    trail_buffer = cfg.get("trail_buffer_cts", 2) / 100.0
    hedge_enabled = cfg.get("hedge_enabled", True)

    window_start = market["window_start"]
    window_end = market["window_end"]
    window_id = f"{coin}-{int(window_start.timestamp())}"
    yes_token = market["yes_token"]
    no_token = market["no_token"]

    positions = WindowPositions(window_id)
    winning_mid_at_phase2: float | None = None
    direction_at_phase2: str | None = None
    last_st: dict = {}

    log.info("scalper_window_started", coin=coin, window_id=window_id,
             window_end=str(window_end), paper=paper)

    while True:
        now = datetime.now(timezone.utc)
        secs_left = (window_end - now).total_seconds()
        if secs_left <= 0:
            break

        try:
            st = await get_stoplicht_dict(coin, yes_token, no_token)
            last_st = st
            yes_no_dir = ("YES" if st.get("direction") == "UP"
                          else "NO" if st.get("direction") == "DOWN" else None)
            await _save_stoplicht_state(coin, st.get("color", "ROOD"), yes_no_dir, st.get("score", 0.0))
        except Exception:
            await asyncio.sleep(1.0)
            continue

        lead = get_winning_side(yes_token, no_token)

        # Snapshot at Phase 2 boundary
        if winning_mid_at_phase2 is None and secs_left <= phase2_secs:
            if lead:
                winning_mid_at_phase2 = lead["winning_mid"]
                direction_at_phase2 = lead["direction"]

        if secs_left > phase2_secs:
            await _phase1_tick(coin, market, positions, st, lead, secs_left,
                               trail_activate, trail_buffer, hedge_enabled,
                               hold_threshold, phase2_secs)
        else:
            await _phase2_tick(coin, market, positions, st, lead, secs_left,
                               trail_activate, trail_buffer, hold_threshold)

        await asyncio.sleep(1.0)

    # Force close remaining open positions at window end
    for slot in ("main", "hedge"):
        pos = getattr(positions, slot)
        if pos:
            exit_p = _paper_exit_price(pos.token_direction, market) or pos.entry_price
            if slot == "main":
                positions.close_main(exit_p, "force_exit")
            else:
                positions.close_hedge(exit_p, "force_exit")

    total_pnl = positions.total_pnl
    log.info("scalper_window_closed", coin=coin, window_id=window_id,
             pnl=total_pnl, trades=positions.trades_count, paper=paper)

    await write_window_tradelog({
        "window_id": window_id,
        "coin": coin,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "stoplicht": last_st.get("color", "ROOD"),
        "direction": last_st.get("direction"),
        "pnl_eur": total_pnl,
        "hold_threshold_used": hold_threshold,
        "paper": 1 if paper else 0,
        "trades_in_window": positions.trades_count,
        "winning_mid_at_phase2": winning_mid_at_phase2,
        "direction_at_phase2": direction_at_phase2,
    })

    await _recalibrate_hold_threshold(coin)


async def scalper_loop(coin: str) -> None:
    """Per-coin main loop. Always evaluates stoplicht for dashboard.
    Only trades when mode == 'stoplicht_scalper'.
    """
    cfg = CONFIG.get("stoplicht_scalper", {})
    scalper_coin = cfg.get("coin", "BTC")
    market_filter = cfg.get("market_filter", "btc-updown-15m")
    entry_start_secs = cfg.get("entry_start_secs", 600)
    entry_cutoff_secs = cfg.get("entry_cutoff_secs", 30)
    poll_secs = cfg.get("poll_interval_secs", 10)

    if coin != scalper_coin:
        return

    log.info("scalper_loop_started", coin=coin, filter=market_filter)

    while True:
        try:
            # Always evaluate stoplicht so dashboard shows live data in any mode
            try:
                color, direction, score = await get_stoplicht(coin)
                await _save_stoplicht_state(coin, color, direction, score)
            except Exception:
                pass

            if get_mode() != "stoplicht_scalper":
                await asyncio.sleep(poll_secs)
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
            secs_left_total = (market["window_end"] - now).total_seconds()

            # Too far in the future
            if secs_to_start > entry_start_secs:
                await asyncio.sleep(poll_secs)
                continue

            # Window already passed or almost over
            if secs_to_start < 0 and secs_left_total < entry_cutoff_secs:
                register_window_trade(coin, window_ts)
                await asyncio.sleep(poll_secs)
                continue

            # Claim window to prevent double-entry
            register_window_trade(coin, window_ts)

            # Wait for window start
            if secs_to_start > 0:
                log.debug("scalper_waiting_for_window", coin=coin,
                          secs=round(secs_to_start), window_id=window_ts)
                await asyncio.sleep(max(0.0, secs_to_start))

            paper = cfg.get("paper_mode", True)
            await _run_window(coin, market, paper)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("scalper_loop_error", coin=coin, error=str(exc))

        await asyncio.sleep(poll_secs)
