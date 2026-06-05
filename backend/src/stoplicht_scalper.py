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
from .stoplicht_signals import get_stoplicht_dict
from .position_manager import WindowPositions, get_position_sizes
from .distance_proxy import get_winning_side

_hold_thresholds: dict[str, float] = {}


async def _save_stoplicht_state(coin: str, st_dict: dict) -> None:
    """Persist full stoplicht state dict to dashboard_state for the UI."""
    try:
        payload = json.dumps({
            **st_dict,
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
    sc_cfg = CONFIG.get("stoplicht_scalper", {})
    entry_min = sc_cfg.get("entry_price_min", 0.20)
    entry_max = sc_cfg.get("entry_price_max", 0.80)
    ask = ws_client.get_best_ask(token)
    if ask and entry_min <= ask <= entry_max:
        return min(entry_max, ask + slippage)
    mid = ws_client.get_mid_price(token)
    if mid and entry_min <= mid <= entry_max:
        return mid
    return None


def _paper_exit_price(token_direction: str, market: dict) -> float | None:
    token = market.get("yes_token") if token_direction == "YES" else market.get("no_token")
    if not token:
        return None
    bid = ws_client.get_best_bid(token)
    if bid and 0.01 < bid < 0.99:
        return bid
    mid = ws_client.get_mid_price(token)
    return mid if mid and 0.01 < mid < 0.99 else None


async def _live_entry(token_dir: str, market: dict, size_eur: float) -> float | None:
    """Place a live limit-buy. Returns the limit price placed (used as entry for tracking)."""
    from . import orders as _orders
    token = market.get("yes_token") if token_dir == "YES" else market.get("no_token")
    if not token:
        return None
    slippage = CONFIG.get("fees", {}).get("paper_slippage_per_share", 0.005)
    sc_cfg = CONFIG.get("stoplicht_scalper", {})
    entry_min = sc_cfg.get("entry_price_min", 0.20)
    entry_max = sc_cfg.get("entry_price_max", 0.80)
    min_order_value = sc_cfg.get("min_order_value", 1.10)
    ask = ws_client.get_best_ask(token)
    if not (ask and entry_min <= ask <= entry_max):
        if ask:
            log.warning("scalper_entry_price_guard", token=token[:8], ask=round(ask, 4),
                        min=entry_min, max=entry_max)
        return None
    price = round(min(entry_max, ask + slippage), 3)
    shares = round(size_eur / max(price, 0.01), 2)
    if shares < 0.01 or shares * price < min_order_value:
        log.warning("scalper_entry_below_min_order", token=token[:8],
                    value=round(shares * price, 3), min=min_order_value)
        return None
    resp = await _orders.place_limit_order(token, "BUY", price, shares)
    if resp and (resp.get("orderID") or resp.get("order_id")):
        log.info("scalper_live_buy", token=token[:8], price=price, shares=shares)
        return price
    log.error("scalper_live_buy_failed", token=token[:8], resp=str(resp))
    return None


async def _live_exit(pos, market: dict, breakeven: bool = False,
                    force: bool = False) -> tuple[float, float]:
    """Place a live FAK market sell. Returns (filled_shares, avg_price).

    FAK (Fill-And-Kill) sweeps the available bid liquidity immediately and kills
    the remainder, so a thin orderbook yields a *partial* fill instead of a total
    FOK failure that would strand tokens. filled_shares == 0 means nothing was
    sold — the caller MUST keep the position open and retry, never book it closed.
    """
    from . import orders as _orders
    token = market.get("yes_token") if pos.token_direction == "YES" else market.get("no_token")
    if not token:
        return 0.0, pos.entry_price
    est_price = pos.entry_price if breakeven else (
        ws_client.get_best_bid(token) or pos.peak_price or pos.entry_price)
    shares = round(pos.size_eur / max(pos.entry_price, 0.01), 2)
    min_order_value = CONFIG.get("stoplicht_scalper", {}).get("min_order_value", 1.10)
    if shares < 0.01 or shares * est_price < min_order_value:
        log.warning("scalper_exit_below_min_order", token=token[:8],
                    value=round(shares * est_price, 3), min=min_order_value)
        return 0.0, est_price

    resp = await _orders.place_market_order(token, "SELL", shares, order_type="FAK")
    order_id = (resp.get("order_id") or resp.get("orderID")) if resp else None
    if not order_id:
        log.error("scalper_sell_failed", token=token[:8], shares=shares, force=force,
                  resp=str(resp)[:200] if resp else None)
        return 0.0, est_price

    import asyncio as _asyncio
    await _asyncio.sleep(1.0)

    filled_shares: float | None = None
    fill_price = est_price
    order_info = await _orders.get_order(order_id)
    if isinstance(order_info, dict):
        # Try all known Polymarket CLOB field names for matched quantity
        matched = (order_info.get("size_matched")
                   or order_info.get("sizeMatched")
                   or order_info.get("matched_size")
                   or order_info.get("matchedSize")
                   or order_info.get("sizeFilled")
                   or order_info.get("size_filled")
                   or order_info.get("amount_filled")
                   or order_info.get("amountFilled"))
        if matched is not None:
            try:
                filled_shares = float(matched)
            except (TypeError, ValueError):
                filled_shares = None
        avg = (order_info.get("avgPrice") or order_info.get("avg_price")
               or order_info.get("price") or order_info.get("matchedPrice"))
        try:
            ap = float(avg)
            if 0.0 < ap <= 1.0:
                fill_price = ap
        except (TypeError, ValueError):
            pass
        log.debug("scalper_sell_order_info", token=token[:8], order_id=order_id,
                  info_keys=list(order_info.keys()), matched=matched,
                  status=order_info.get("status"))

    # Fallback to the POST response status when get_order gave no matched size.
    if filled_shares is None:
        raw = (resp or {}).get("raw") or {}
        status = str(raw.get("status") or (resp or {}).get("status") or "").lower()
        filled_shares = shares if status in ("matched", "filled") else 0.0
        log.debug("scalper_sell_status_fallback", token=token[:8], status=status,
                  filled_shares=filled_shares, raw_keys=list(raw.keys()) if raw else [])

    log.info("scalper_live_sell_market", token=token[:8], requested=shares,
             filled=round(filled_shares, 2), fill_price=round(fill_price, 4),
             force=force, breakeven=breakeven)
    return filled_shares, fill_price


def _close_slot(positions, slot: str, exit_price: float, reason: str) -> float:
    if slot == "main":
        return positions.close_main(exit_price, reason)
    return positions.close_hedge(exit_price, reason)


def _apply_live_exit(positions, slot: str, pos, filled_shares: float,
                     avg_price: float, reason: str) -> bool:
    """Close/reduce a slot based on the ACTUAL fill. Returns True if fully closed.

    filled_shares <= 0  → nothing sold; keep position open for retry (return False).
    filled ≥ 95% req    → full close at avg_price.
    partial             → reduce position to the unsold remainder, retry next tick.
    """
    requested = pos.size_eur / max(pos.entry_price, 0.01)
    if filled_shares <= 0:
        log.warning("scalper_exit_unfilled_retry", slot=slot, reason=reason,
                    requested=round(requested, 2))
        return False
    if filled_shares >= requested * 0.95:
        if slot == "main":
            positions.close_main(avg_price, reason)
        else:
            positions.close_hedge(avg_price, reason)
        return True
    if slot == "main":
        positions.reduce_main(filled_shares, avg_price, reason + "_partial")
    else:
        positions.reduce_hedge(filled_shares, avg_price, reason + "_partial")
    log.info("scalper_exit_partial", slot=slot, reason=reason,
             filled=round(filled_shares, 2), requested=round(requested, 2))
    return False


def _get_token_mid(token_direction: str, market: dict) -> float | None:
    token = market.get("yes_token") if token_direction == "YES" else market.get("no_token")
    return ws_client.get_mid_price(token) if token else None


def _should_hold_for_spread(pos, market: dict) -> bool:
    """True when spread ≥ spread_hold_cts AND position is on the winning side (mid > 0.50).
    In that case selling at bid would waste the spread; holding to $1 resolution is better.
    """
    threshold = CONFIG.get("stoplicht_scalper", {}).get("spread_hold_cts", 4) / 100.0
    token = market.get("yes_token") if pos.token_direction == "YES" else market.get("no_token")
    if not token:
        return False
    bid = ws_client.get_best_bid(token)
    ask = ws_client.get_best_ask(token)
    if bid is None or ask is None:
        return False
    if (ask - bid) < threshold:
        return False
    mid = ws_client.get_mid_price(token)
    return bool(mid and mid > 0.50)


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
                       trail_activate, trail_buffer, hedge_enabled, hold_threshold,
                       phase2_secs, paper: bool = True):
    """Phase 1 tick: manage open positions + try new entries on full consensus."""
    sizes = get_position_sizes()

    # ── Manage main position ───────────────────────────────────────────────────
    if positions.main:
        pos = positions.main
        should_exit, current_mid = _tick_trailing(pos, market, trail_activate, trail_buffer)

        # MOM reversal: stoplicht flipped to opposite direction with conviction.
        # No spread-hold here: Phase 1 is active scalping — signals always fire.
        new_dir = st.get("direction")
        if new_dir and new_dir != pos.direction and st.get("score", 0) >= 0.40:
            be = not pos.trailing_active  # trailing nooit bereikt → break-even exit
            reason = "mom_reversal_be" if be else "mom_reversal"
            if paper:
                exit_p = pos.entry_price if be else (
                    _paper_exit_price(pos.token_direction, market) or current_mid or pos.entry_price)
                pnl = positions.close_main(exit_p, reason)
                log.info("scalper_mom_exit", coin=coin, pnl=pnl, secs_left=round(secs_left),
                         paper=paper, breakeven=be)
            else:
                filled, avg = await _live_exit(pos, market, breakeven=be)
                if _apply_live_exit(positions, "main", pos, filled, avg, reason):
                    log.info("scalper_mom_exit", coin=coin, secs_left=round(secs_left),
                             paper=paper, breakeven=be)
            return

        if should_exit and current_mid:
            if paper:
                exit_p = _paper_exit_price(pos.token_direction, market) or current_mid
                pnl = positions.close_main(exit_p, "trail_stop")
                log.info("scalper_trail_exit", coin=coin, pnl=pnl, secs_left=round(secs_left), paper=paper)
            else:
                filled, avg = await _live_exit(pos, market)
                if _apply_live_exit(positions, "main", pos, filled, avg, "trail_stop"):
                    log.info("scalper_trail_exit", coin=coin, secs_left=round(secs_left), paper=paper)
            return

    # ── Manage hedge position ──────────────────────────────────────────────────
    if positions.hedge:
        pos = positions.hedge
        should_exit, current_mid = _tick_trailing(pos, market, trail_activate, trail_buffer)
        if should_exit and current_mid:
            if paper:
                exit_p = _paper_exit_price(pos.token_direction, market) or current_mid
                positions.close_hedge(exit_p, "trail_stop")
            else:
                filled, avg = await _live_exit(pos, market)
                _apply_live_exit(positions, "hedge", pos, filled, avg, "trail_stop")

    # ── ROOD = harde geen entry ────────────────────────────────────────────────
    if st.get("color") == "ROOD":
        return

    # ── Open main position: full consensus + market confirmation ───────────────
    if positions.can_open_main() and st.get("consensus") and st.get("confirmed"):
        direction = st.get("direction")
        if direction:
            token_dir = "YES" if direction == "UP" else "NO"
            if paper:
                entry_p = _paper_entry_price(token_dir, market)
            else:
                entry_p = await _live_entry(token_dir, market, sizes["main_eur"])
            if entry_p:
                positions.open_main(direction, entry_p, sizes["main_eur"])
                log.info("scalper_entry", coin=coin, direction=direction,
                         entry=entry_p, size=sizes["main_eur"], secs_left=round(secs_left), paper=paper)

    # ── Contrarian hedge: support wall in sight, enough time, market beweeglijk ─
    if (hedge_enabled
            and positions.can_open_hedge()
            and st.get("support_near")
            and secs_left > phase2_secs + 60
            and lead and lead["winning_mid"] < hold_threshold):
        bounce_dir = st.get("support_bounce_direction")
        if bounce_dir:
            token_dir = "YES" if bounce_dir == "UP" else "NO"
            if paper:
                entry_p = _paper_entry_price(token_dir, market)
            else:
                entry_p = await _live_entry(token_dir, market, sizes["hedge_eur"])
            if entry_p:
                positions.open_hedge(bounce_dir, entry_p, sizes["hedge_eur"])
                log.info("scalper_hedge_entry", coin=coin, direction=bounce_dir,
                         entry=entry_p, paper=paper)

    # ── Snap reversal hedge: confirmed hard BTC move → bet on reversal ─────────
    snap = st.get("snap_hedge")
    if (snap and hedge_enabled
            and positions.can_open_hedge()
            and secs_left > phase2_secs + 30
            and snap.get("confidence", 0) >= 0.35):
        snap_dir   = snap["hedge_direction"]  # opposite of the snap
        token_dir  = "YES" if snap_dir == "UP" else "NO"
        snap_size  = round(sizes["main_eur"] * 0.50, 2)
        if paper:
            entry_p = _paper_entry_price(token_dir, market)
        else:
            entry_p = await _live_entry(token_dir, market, snap_size)
        if entry_p:
            positions.open_hedge(snap_dir, entry_p, snap_size)
            log.info("scalper_snap_hedge", coin=coin, direction=snap_dir,
                     snap_direction=snap.get("snap_direction"),
                     magnitude=snap.get("snap_magnitude"),
                     near_round=snap.get("near_round"),
                     confidence=snap.get("confidence"),
                     prior_count=snap.get("prior_count"),
                     size_eur=snap_size, paper=paper)


async def _phase2_tick(coin, market, positions, st, lead, secs_left,
                       trail_activate, trail_buffer, hold_threshold, paper: bool = True):
    """Phase 2 tick: hold if winning side strong, else take final position."""
    sizes = get_position_sizes()

    if lead and lead["winning_mid"] >= hold_threshold:
        # Hold mode: trailing still active, no MOM exits, no new entries
        for slot in ("main", "hedge"):
            pos = getattr(positions, slot)
            if pos:
                should_exit, current_mid = _tick_trailing(pos, market, trail_activate, trail_buffer)
                if should_exit and current_mid:
                    if paper:
                        exit_p = _paper_exit_price(pos.token_direction, market) or current_mid
                        if slot == "main":
                            positions.close_main(exit_p, "trail_stop_p2")
                        else:
                            positions.close_hedge(exit_p, "trail_stop_p2")
                    else:
                        filled, avg = await _live_exit(pos, market)
                        _apply_live_exit(positions, slot, pos, filled, avg, "trail_stop_p2")
        return

    # No hold signal — take final directional position if consensus, no current position
    # ROOD = harde geen entry, ook in Phase 2
    if positions.can_open_main() and st.get("consensus") and st.get("color") != "ROOD":
        direction = st.get("direction")
        if direction:
            token_dir = "YES" if direction == "UP" else "NO"
            if paper:
                entry_p = _paper_entry_price(token_dir, market)
            else:
                entry_p = await _live_entry(token_dir, market, sizes["main_eur"])
            if entry_p:
                positions.open_main(direction, entry_p, sizes["main_eur"])
                log.info("scalper_phase2_entry", coin=coin, direction=direction,
                         entry=entry_p, secs_left=round(secs_left), paper=paper)

    # Continue trailing existing positions
    for slot in ("main", "hedge"):
        pos = getattr(positions, slot)
        if pos:
            should_exit, current_mid = _tick_trailing(pos, market, trail_activate, trail_buffer)
            if should_exit and current_mid:
                if paper:
                    exit_p = _paper_exit_price(pos.token_direction, market) or current_mid
                    _close_slot(positions, slot, exit_p, "trail_stop_p2")
                else:
                    filled, avg = await _live_exit(pos, market)
                    _apply_live_exit(positions, slot, pos, filled, avg, "trail_stop_p2")


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
             window_end=str(window_end), paper=paper,
             mode="PAPER" if paper else "LIVE *** REAL ORDERS ***")

    while True:
        now = datetime.now(timezone.utc)
        secs_left = (window_end - now).total_seconds()
        if secs_left <= 0:
            break

        try:
            st = await get_stoplicht_dict(coin, yes_token, no_token)
            last_st = st
            await _save_stoplicht_state(coin, st)
        except Exception:
            await asyncio.sleep(1.0)
            continue

        lead = get_winning_side(yes_token, no_token)

        # Save live window state for dashboard (time remaining, prices, position)
        try:
            yes_mid = ws_client.get_mid_price(yes_token)
            no_mid  = ws_client.get_mid_price(no_token)
            pos = positions.main
            await save_dashboard_state(f"scalper_window_{coin}", json.dumps({
                "window_id":  window_id,
                "window_end": window_end.isoformat(),
                "secs_left":  round(secs_left),
                "phase2":     secs_left <= phase2_secs,
                "yes_mid":    yes_mid,
                "no_mid":     no_mid,
                "has_position":        pos is not None,
                "position_direction":  pos.direction    if pos else None,
                "position_entry":      pos.entry_price  if pos else None,
                "position_peak":       pos.peak_price   if pos else None,
                "position_size_eur":   pos.size_eur     if pos else None,
                "trailing_active":     pos.trailing_active if pos else False,
                "trades_in_window":    positions.trades_count,
                "running_pnl":         positions.total_pnl,
            }))
        except Exception:
            pass

        # Snapshot at Phase 2 boundary
        if winning_mid_at_phase2 is None and secs_left <= phase2_secs:
            if lead:
                winning_mid_at_phase2 = lead["winning_mid"]
                direction_at_phase2 = lead["direction"]

        if secs_left > phase2_secs:
            await _phase1_tick(coin, market, positions, st, lead, secs_left,
                               trail_activate, trail_buffer, hedge_enabled,
                               hold_threshold, phase2_secs, paper)
        else:
            await _phase2_tick(coin, market, positions, st, lead, secs_left,
                               trail_activate, trail_buffer, hold_threshold, paper)

        await asyncio.sleep(1.0)

    # Cancel any open (unfilled GTC) orders before force-exit so tokens are free to sell
    if not paper:
        try:
            from . import orders as _orders
            await _orders.cancel_all_orders()
            log.debug("scalper_window_orders_cancelled", coin=coin, window_id=window_id)
        except Exception as _cancel_exc:
            log.warning("scalper_cancel_orders_failed", coin=coin, error=str(_cancel_exc))

    # Force close remaining open positions at window end
    for slot in ("main", "hedge"):
        pos = getattr(positions, slot)
        if not pos:
            continue

        if _should_hold_for_spread(pos, market):
            # Winning position + wide spread → hold to $1 resolution, no sell order
            log.info("scalper_hold_resolution", coin=coin, slot=slot,
                     entry=pos.entry_price, paper=paper)
            _close_slot(positions, slot, 1.0, "hold_resolution")
            continue

        be = not pos.trailing_active
        reason = "force_exit_be" if be else "force_exit"

        if paper:
            exit_p = pos.entry_price if be else (
                _paper_exit_price(pos.token_direction, market) or pos.entry_price)
            _close_slot(positions, slot, exit_p, reason)
            continue

        # Live: FAK sweep. Whatever can't be sold (no bids) settles at $1/$0.
        tok = market.get("yes_token") if pos.token_direction == "YES" else market.get("no_token")
        filled, avg = await _live_exit(pos, market, breakeven=be, force=True)
        if _apply_live_exit(positions, slot, pos, filled, avg, reason):
            continue

        # 0 fill — check on-chain balance: if tokens are gone, user sold manually.
        if tok and not paper:
            from . import orders as _orders_bal
            actual_balance = await _orders_bal.get_token_balance(tok)
            if actual_balance < 0.01:
                log.info("scalper_position_externally_closed", coin=coin, slot=slot,
                         entry=pos.entry_price, booked_at="entry_price")
                _close_slot(positions, slot, pos.entry_price, "externally_closed")
                continue

        # Remainder unsold and still on-chain — book at binary resolution.
        res_mid = ws_client.get_mid_price(tok) if tok else None
        res_price = 1.0 if (res_mid is not None and res_mid >= 0.5) else 0.0
        log.info("scalper_exit_to_resolution", coin=coin, slot=slot,
                 entry=pos.entry_price, resolves_to=res_price, paper=paper)
        _close_slot(positions, slot, res_price, "resolution")

    total_pnl = positions.total_pnl
    closed = positions._closed
    entry_price = closed[0]["entry"] if closed else None
    exit_price  = closed[-1]["exit"] if closed else None
    exit_reason = closed[-1]["reason"] if closed else None

    log.info("scalper_window_closed", coin=coin, window_id=window_id,
             pnl=total_pnl, trades=positions.trades_count, paper=paper)

    await write_window_tradelog({
        "window_id": window_id,
        "coin": coin,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "stoplicht": last_st.get("color", "ROOD"),
        "direction": last_st.get("direction"),
        "entry_price": entry_price,
        "exit_price":  exit_price,
        "exit_reason": exit_reason,
        "pnl_eur": total_pnl,
        "hold_threshold_used": hold_threshold,
        "paper": 1 if paper else 0,
        "trades_in_window": positions.trades_count,
        "winning_mid_at_phase2": winning_mid_at_phase2,
        "direction_at_phase2": direction_at_phase2,
    })

    # Clear live window state
    try:
        await save_dashboard_state(f"scalper_window_{coin}", json.dumps({
            "secs_left": 0, "window_id": window_id,
            "window_end": window_end.isoformat(),
        }))
    except Exception:
        pass

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
                from .stoplicht_signals import get_stoplicht_dashboard as _gsd
                _dash = await _gsd(coin)
                await _save_stoplicht_state(coin, _dash)
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
