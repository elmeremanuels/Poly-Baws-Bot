"""Bot orchestration — runs the trading loop for all coins."""
import asyncio
from datetime import datetime, timezone

from . import scanner, ws_client, risk, asset_price_feed, signals as _signals, weighting_guard as _wg
from . import whale_tracker as _whale_tracker
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


def _in_skip_hours(window_start) -> bool:
    """Return True if window_start falls in a configured skip-hour (UTC).

    CONFIG.trading_hours.skip_utc_hours: list of UTC hours to block (e.g. [5, 7]).
    Data from 27-05-2026 shows hour 05 (41% WR, -€8.21) and hour 07 (43% WR, -€6.73)
    are consistent losers; hours 02 (83%) and 04 (87%) are the best.
    """
    th_cfg = CONFIG.get("trading_hours", {})
    if not th_cfg.get("enabled", False):
        return False
    skip_hours = th_cfg.get("skip_utc_hours", [])
    if not skip_hours or window_start is None:
        return False
    utc_hour = window_start.hour if hasattr(window_start, "hour") else None
    return utc_hour in skip_hours


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


async def _process_coin_window(coin: str, market: dict, force_paper: bool = False) -> None:
    # force_paper=True: achtergrond Signal Lab straddle terwijl signal_trader actief is.
    # Gebruikt mode="paper" zodat execute_entry altijd simuleert, ongeacht live/paper instelling.
    mode = "paper" if force_paper else get_mode()

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

    if _in_skip_hours(market.get("window_start")):
        log.info("trade_skipped_hour_filter", coin=coin,
                 hour=market["window_start"].hour if market.get("window_start") else None)
        return

    # In auto mode OR force_paper (background signal lab): verify entry conditions BEFORE
    # consuming the window slot. If the check fails (e.g. no orderbook data yet), the
    # window stays available so the next iteration (10s later) can retry.
    if force_paper or _effective_is_auto(mode):
        ok_entry, entry_reason = _should_enter(market)
        if not ok_entry:
            log.info("trade_skipped_spread", coin=coin, reason=entry_reason)
            return  # window NOT registered — retried next iteration

    triggered_by = "signal_lab_bg" if force_paper else "bot"
    trade = create_trade_state(coin, market, mode, triggered_by=triggered_by)

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

    if not force_paper and not _effective_is_auto(mode):
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

    # auto_router final re-validation at T-2min: abort if signals changed
    if get_mode() == "auto_router" and market.get("window_start"):
        now = datetime.now(timezone.utc)
        mins_to_start = (market["window_start"] - now).total_seconds() / 60
        if mins_to_start <= 2.5:
            from .trade_router import route_trade
            final_decision = route_trade(coin, market)
            if final_decision.bucket == "skip":
                log.info(
                    "trade_aborted_final_check",
                    coin=coin, trade_id=trade_id, reason=final_decision.skip_reason,
                )
                from .state import remove_active_trade
                remove_active_trade(trade_id)
                return

    await write_event(trade_id, "entry_initiated", coin, {"mode": mode})
    success = await execute_entry(trade_id)
    if not success:
        log.info("entry_failed", coin=coin, trade_id=trade_id)


async def _coin_loop(coin: str) -> None:
    while True:
        if get_mode() == "signal_trader":
            # Achtergrond paper straddle — houdt Signal Lab data vers terwijl
            # Signal Trader actief is. Handelt ALLEEN in:
            #   • windows die Signal Trader niet heeft geclaimd (has_traded_window=False)
            #   • binnen 20 min voor window-start zodat Signal Trader 25+ min
            #     voorrang heeft om zijn conviction-check te doen
            # Trades worden opgeslagen als mode="paper", triggered_by="signal_lab_bg"
            # zodat Signal Lab ze meeneemt in de per-coin accuraatheid.
            if not risk.is_killed():
                try:
                    market = scanner.get_tradeable_market(coin)
                    if market and market.get("window_start"):
                        now = datetime.now(timezone.utc)
                        mins_to_start = (market["window_start"] - now).total_seconds() / 60
                        if mins_to_start <= 20:
                            window_ts = market["window_start"].isoformat()
                            if not has_traded_window(coin, window_ts):
                                await _process_coin_window(coin, market, force_paper=True)
                except Exception as e:
                    log.error("bg_paper_loop_error", coin=coin, error=str(e))
            await asyncio.sleep(10)
            continue

        if get_mode() == "auto_router":
            if not risk.is_killed():
                try:
                    await _auto_router_coin_tick(coin)
                except Exception as e:
                    log.error("auto_router_loop_error", coin=coin, error=str(e))
            await asyncio.sleep(10)
            continue

        if get_mode() in ("bggdsb_paper", "bggdsb_live"):
            if not risk.is_killed():
                try:
                    await _bggdsb_coin_tick(coin)
                except Exception as e:
                    log.error("bggdsb_loop_error", coin=coin, error=str(e))
            await asyncio.sleep(10)
            continue

        if risk.is_killed():
            # Oracle paper_continues_on_kill: switch to paper so Oracle keeps learning
            if CONFIG.get("oracle", {}).get("paper_continues_on_kill", True):
                mode = get_mode()
                if not mode.startswith("paper"):
                    try:
                        from .state import set_mode
                        set_mode("paper_auto")
                        log.info("kill_active_paper_continues", previous_mode=mode)
                    except Exception:
                        pass
                # Fall through — paper trades continue
            else:
                await asyncio.sleep(5)
                continue
        try:
            market = scanner.get_tradeable_market(coin)
            if market:
                await _process_coin_window(coin, market)
        except Exception as e:
            log.error("coin_loop_error", coin=coin, error=str(e))
        await asyncio.sleep(10)


async def _auto_router_coin_tick(coin: str) -> None:
    """One scan tick for auto_router mode: route → execute in the matching bucket."""
    from .trade_router import route_trade

    market = scanner.get_tradeable_market(coin)
    if not market:
        return

    window_ts = market["window_start"].isoformat() if market.get("window_start") else ""
    if has_traded_window(coin, window_ts):
        return

    decision = route_trade(coin, market)
    log.info(
        "route_decision",
        coin=coin,
        bucket=decision.bucket,
        conviction=decision.conviction_score,
        regime=decision.regime,
        skip_reason=decision.skip_reason or None,
    )

    if decision.bucket == "skip":
        return

    # ── Oracle gate (Fase 2) ───────────────────────────────────────────────────
    import uuid as _uuid
    from . import oracle as _oracle_mod
    pre_trade_id = str(_uuid.uuid4())
    try:
        verdict = await _oracle_mod.get_oracle_verdict(
            coin=coin,
            conviction_score=decision.conviction_score,
            regime=decision.regime,
            trade_id=pre_trade_id,
        )
        if not verdict.approved:
            log.info(
                "oracle_blocked_trade",
                coin=coin,
                reason=verdict.reason,
                temperature=verdict.trading_temperature,
                trade_id=pre_trade_id,
            )
            return
    except Exception as _exc:
        log.warning("oracle_gate_error", coin=coin, error=str(_exc))
        # Never block a trade on Oracle errors — fail open

    if decision.bucket == "signal":
        # Delegate to signal_trader's own check — it re-validates internally
        from . import signal_trader as _st
        await _st._check_and_trade(coin)
        return

    # straddle_asym or straddle_sym — route through standard process_coin_window
    # Stamp the routing decision so _process_coin_window can use the sized stakes
    market["_router_yes_size"] = decision.yes_size
    market["_router_no_size"] = decision.no_size
    market["_router_bucket"] = decision.bucket
    market["_router_conviction_score"] = decision.conviction_score
    market["_pre_trade_id"] = pre_trade_id   # links trade to oracle verdict
    # Respect router.paper_mode flag — default True so auto_router starts safe
    _router_paper = CONFIG.get("router", {}).get("paper_mode", True)
    # coin_guard paper_only state always forces paper regardless of global setting
    from . import coin_guard as _cg
    if _cg.is_paper_forced(coin):
        _router_paper = True
    # Oracle per-coin temperature gate: if too cold for this coin → force paper
    _oracle_paper = await _oracle_coin_paper_gate(coin, decision.conviction_score, decision.regime)
    if _oracle_paper:
        _router_paper = True
    await _process_coin_window(coin, market, force_paper=_router_paper)


# Tranche state: {f"{coin}_{window_ts}": {placed, last_ts, max_tranches, tranche_eur, dominant_side}}
# Module level state for bggdsb dynamic winner-chase strategy
_bggdsb_tranche_state: dict[str, dict] = {}   # window_key -> window state
_bggdsb_flip_tasks: dict[str, asyncio.Task] = {}  # window_key -> flip task


async def _bggdsb_extra_buy(
    coin: str,
    side: str,
    token: str,
    shares: float,
    buy_eur: float,
    is_paper: bool,
    window_key: str,
) -> bool:
    """Directe market buy voor flip/confirm/hedge — maakt geen aparte trade aan."""
    from . import paper_trader as _pt, orders as _ord
    try:
        if is_paper:
            result = await _pt.simulate_market_buy(token, shares)
            filled = result.get("filled", True)
        else:
            resp = await _ord.place_market_order(token, "BUY", shares)
            filled = bool(resp and resp.get("order_id"))
            if filled:
                for _ in range(3):
                    await asyncio.sleep(0.8)
                    order = await _ord.get_order(resp["order_id"])
                    if order and order.get("status") in ("MATCHED", "FILLED"):
                        break
        if filled:
            st = _bggdsb_tranche_state.get(window_key)
            if st:
                key = "yes_spend" if side == "YES" else "no_spend"
                st[key] = st.get(key, 0.0) + buy_eur
            log.info("bggdsb_extra_buy", coin=coin, side=side,
                     shares=round(shares, 2), eur=round(buy_eur, 2), paper=is_paper)
            return True
    except Exception as e:
        log.error("bggdsb_extra_buy_error", coin=coin, side=side, error=str(e))
    return False


async def _bggdsb_window_flip_task(window_key: str) -> None:
    """
    is5minfixedyet strategie — dynamische winnaar-chase per window.

    Fases:
      monitoring  → wacht op flip-signaal (andere kant > flip_threshold en > dom kant)
      flipping    → koop andere kant tot break-even bereikt
      confirmed   → break-even bereikt, koop extra op winnaar
      done        → window verlopen
    """
    import json as _json
    from .db_sync import set_dashboard_state as _sds
    cfg = CONFIG.get("bggdsb", {})
    flip_threshold    = float(cfg.get("flip_trigger_price", 0.50))
    confirm_threshold = float(cfg.get("confirm_trigger_price", 0.60))
    confirm_secs      = float(cfg.get("confirm_window_secs", 90))
    # Schaal tranches proportioneel met budget (basis: €8 flip / €15 confirm bij €30)
    _scale            = window_budget / 30.0
    flip_tranche_eur  = round(float(cfg.get("flip_tranche_eur", 8.0)) * _scale, 1)
    confirm_eur       = round(float(cfg.get("confirm_tranche_eur", 15.0)) * _scale, 1)
    hedge_price       = float(cfg.get("hedge_price_trigger", 0.11))
    max_mult          = float(cfg.get("max_budget_multiplier", 2.5))
    flip_interval     = float(cfg.get("flip_interval_secs", 3.0))
    dashboard_interval = 3.0  # seconden tussen dashboard updates

    try:
        st = _bggdsb_tranche_state.get(window_key)
        if not st:
            return

        dominant_side = st["dominant_side"]
        other_side    = "NO" if dominant_side == "YES" else "YES"
        yes_token     = st["yes_token"]
        no_token      = st["no_token"]
        window_end_dt = st["window_end_dt"]
        is_paper      = st["is_paper"]
        coin          = st["coin"]
        window_budget = st["window_budget"]
        max_spend     = window_budget * max_mult

        hedge_placed  = False
        confirm_done  = False
        last_flip_t   = 0.0
        last_dash_t   = 0.0
        phase         = "monitoring"

        log.info("bggdsb_flip_task_start", coin=coin, window_key=window_key,
                 dominant_side=dominant_side, budget=window_budget)

        while True:
            now = datetime.now(timezone.utc)
            secs_left = (window_end_dt - now).total_seconds()
            if secs_left <= 1:
                break

            loop_t = asyncio.get_event_loop().time()

            st = _bggdsb_tranche_state.get(window_key)
            if not st:
                break

            yes_spend   = st.get("yes_spend", 0.0)
            no_spend    = st.get("no_spend", 0.0)
            total_spend = yes_spend + no_spend

            yes_mid = ws_client.get_mid_price(yes_token)
            no_mid  = ws_client.get_mid_price(no_token)
            if yes_mid is None or no_mid is None:
                await asyncio.sleep(1.0)
                continue

            other_mid   = no_mid  if other_side == "NO"  else yes_mid
            dom_mid     = yes_mid if dominant_side == "YES" else no_mid
            other_token = no_token if other_side == "NO" else yes_token
            other_spend = no_spend if other_side == "NO" else yes_spend

            # ── FLIP: andere kant is nu favoriet ──────────────────────────
            if (other_mid > flip_threshold
                    and other_mid > dom_mid
                    and total_spend < max_spend
                    and loop_t - last_flip_t >= flip_interval):

                # Break-even berekening: other_shares × €1 ≥ total_spend
                # Nodig: other_spend ≥ total_spend × other_mid
                be_needed = total_spend * other_mid

                if other_spend < be_needed:
                    phase = "flipping"
                    shortfall = be_needed - other_spend
                    buy_eur = min(flip_tranche_eur, shortfall + 5.0)
                    buy_eur = min(buy_eur, max_spend - total_spend)
                    if buy_eur >= 1.0:
                        other_ask = ws_client.get_best_ask(other_token) or other_mid
                        shares = round(buy_eur / max(other_ask, 0.01), 2)
                        if shares >= 0.1:
                            ok = await _bggdsb_extra_buy(
                                coin, other_side, other_token,
                                shares, buy_eur, is_paper, window_key
                            )
                            if ok:
                                last_flip_t = loop_t
                                other_spend += buy_eur
                else:
                    if phase == "flipping":
                        phase = "confirmed"
                        log.info("bggdsb_breakeven_reached", coin=coin,
                                 total_spend=round(total_spend, 2),
                                 other_spend=round(other_spend, 2),
                                 other_mid=round(other_mid, 4))
                    # Extra kopen na break-even voor meer winst
                    if (total_spend < max_spend * 0.85
                            and loop_t - last_flip_t >= flip_interval * 2):
                        buy_eur = min(flip_tranche_eur, max_spend - total_spend)
                        if buy_eur >= 2.0:
                            other_ask = ws_client.get_best_ask(other_token) or other_mid
                            shares = round(buy_eur / max(other_ask, 0.01), 2)
                            if shares >= 0.1:
                                await _bggdsb_extra_buy(
                                    coin, other_side, other_token,
                                    shares, buy_eur, is_paper, window_key
                                )
                                last_flip_t = loop_t

            # ── CONFIRM: laatste N seconden, duidelijke winnaar ────────────
            if (not confirm_done
                    and secs_left <= confirm_secs
                    and total_spend < max_spend):
                winner_mid  = max(yes_mid, no_mid)
                winner_side = "YES" if yes_mid >= no_mid else "NO"
                if winner_mid >= confirm_threshold:
                    winner_tok = yes_token if winner_side == "YES" else no_token
                    buy_eur = min(confirm_eur, max_spend - total_spend)
                    if buy_eur >= 2.0:
                        w_ask = ws_client.get_best_ask(winner_tok) or winner_mid
                        shares = round(buy_eur / max(w_ask, 0.01), 2)
                        if shares >= 0.1:
                            ok = await _bggdsb_extra_buy(
                                coin, winner_side, winner_tok,
                                shares, buy_eur, is_paper, window_key
                            )
                            if ok:
                                confirm_done = True
                                log.info("bggdsb_confirm_buy", coin=coin,
                                         winner=winner_side, mid=round(winner_mid, 4),
                                         eur=round(buy_eur, 2))

            # ── HEDGE: goedkope kant ≤ hedge_price ────────────────────────
            if not hedge_placed:
                loser_mid  = min(yes_mid, no_mid)
                if 0 < loser_mid <= hedge_price:
                    loser_side = "YES" if yes_mid <= no_mid else "NO"
                    loser_tok  = yes_token if loser_side == "YES" else no_token
                    hedge_eur  = window_budget * 0.05
                    hedge_eur  = min(hedge_eur, max_spend - total_spend)
                    if hedge_eur >= 0.5:
                        l_ask   = ws_client.get_best_ask(loser_tok) or loser_mid
                        shares  = round(hedge_eur / max(l_ask, 0.01), 2)
                        if shares >= 0.1:
                            ok = await _bggdsb_extra_buy(
                                coin, loser_side, loser_tok,
                                shares, hedge_eur, is_paper, window_key
                            )
                            if ok:
                                hedge_placed = True

            # ── State bijwerken + dashboard ────────────────────────────────
            if st := _bggdsb_tranche_state.get(window_key):
                st["phase"]        = phase
                st["hedge_placed"] = hedge_placed
                st["confirm_done"] = confirm_done
                st["yes_mid"]      = round(yes_mid, 4)
                st["no_mid"]       = round(no_mid, 4)
                st["secs_left"]    = round(secs_left, 0)

            if loop_t - last_dash_t >= dashboard_interval:
                st2 = _bggdsb_tranche_state.get(window_key)
                if st2:
                    _sds("bggdsb_active_window", _json.dumps({
                        "coin": coin,
                        "window_key": window_key,
                        "phase": phase,
                        "dominant_side": dominant_side,
                        "yes_spend": round(st2.get("yes_spend", 0), 2),
                        "no_spend":  round(st2.get("no_spend", 0), 2),
                        "yes_mid":   round(yes_mid, 4),
                        "no_mid":    round(no_mid, 4),
                        "secs_left": round(secs_left, 0),
                        "be_needed": round(total_spend * other_mid, 2),
                        "breakeven_reached": phase in ("confirmed",),
                    }))
                last_dash_t = loop_t

            await asyncio.sleep(1.5)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error("bggdsb_flip_task_error", window_key=window_key, error=str(e))
    finally:
        _bggdsb_flip_tasks.pop(window_key, None)
        st = _bggdsb_tranche_state.get(window_key)
        if st:
            log.info("bggdsb_window_done", window_key=window_key,
                     yes_spend=round(st.get("yes_spend", 0), 2),
                     no_spend=round(st.get("no_spend", 0), 2),
                     phase=st.get("phase", "?"))
            from .state import register_window_trade
            register_window_trade(coin, st.get("window_ts", ""))
            _bggdsb_tranche_state.pop(window_key, None)
        from .db_sync import set_dashboard_state as _sds2
        _sds2("bggdsb_active_window", "")


async def _bggdsb_coin_tick(coin: str) -> None:
    """
    is5minfixedyet strategie 1:1:
    1. Market competitive gate (0.10-0.90 beide kanten)
    2. Conviction entry: koop dominant kant voor volledig budget
    3. Start window flip task die dynamisch de winnaar achtervolgt
    """
    import json as _json
    from .db_sync import get_state as _get_state
    from .state import register_window_trade, create_trade_state, add_active_trade
    from .triggers import execute_entry

    bggdsb_cfg = CONFIG.get("bggdsb", {})

    # Coin whitelist
    _coins_raw = _get_state("bggdsb_coins")
    try:
        allowed_coins = _json.loads(_coins_raw) if _coins_raw else bggdsb_cfg.get("coins", ["BTC"])
    except Exception:
        allowed_coins = bggdsb_cfg.get("coins", ["BTC"])
    if coin not in allowed_coins:
        return

    market = scanner.get_tradeable_market(coin)
    if not market or not market.get("window_start"):
        return

    window_ts  = market["window_start"].isoformat()
    window_key = f"{coin}_{window_ts}"
    now        = datetime.now(timezone.utc)

    # Al een flip task bezig voor dit window? Dan niets doen.
    if window_key in _bggdsb_flip_tasks:
        return

    if has_traded_window(coin, window_ts):
        return

    # Timing — wacht op entry_delay zodat prijzen gezet zijn, enter dan met meest recente data
    entry_window_secs = bggdsb_cfg.get("entry_window_secs", 90)
    entry_delay_secs  = bggdsb_cfg.get("entry_delay_secs", 45)
    secs_since_start  = (now - market["window_start"]).total_seconds()
    if secs_since_start < entry_delay_secs:
        return  # te vroeg — wacht op meer prijsdata
    if secs_since_start > entry_window_secs or secs_since_start < -300:
        return

    paper_raw   = _get_state("bggdsb_paper_mode") or ("1" if bggdsb_cfg.get("paper_mode", True) else "0")
    force_paper = bool(int(paper_raw))

    yes_token = market.get("yes_token", "")
    no_token  = market.get("no_token", "")
    yes_ask   = ws_client.get_best_ask(yes_token)
    no_ask    = ws_client.get_best_ask(no_token)
    if yes_ask is None or no_ask is None:
        return

    # Market competitive gate
    comp_min = bggdsb_cfg.get("entry_price_min", 0.10)
    comp_max = bggdsb_cfg.get("entry_price_max", 0.90)
    if not (comp_min <= yes_ask <= comp_max and comp_min <= no_ask <= comp_max):
        return

    # is5 signaalgewicht
    is5_weight = 0
    try:
        is5_weight = int(_get_state("bggdsb_is5_signal_weight") or "0")
    except ValueError:
        pass

    if is5_weight > 0:
        from .db_sync import is5_recently_active
        if not is5_recently_active(coin=coin, minutes=15):
            return

    # Conviction
    from . import signals as _sigs
    conv_dir, conv_score = _sigs.get_conviction(coin)
    base_thr = 0.35
    adj_thr  = base_thr - (is5_weight / 100.0) * 0.20
    if not conv_dir or conv_score < adj_thr:
        log.info("bggdsb_skip_low_conviction", coin=coin,
                 score=conv_score, threshold=adj_thr)
        return

    dominant_side = "YES" if conv_dir == "UP" else "NO"

    # Budget
    try:
        budget_eur = float(_get_state("bggdsb_window_budget") or bggdsb_cfg.get("window_budget_eur", 30.0))
    except (ValueError, TypeError):
        budget_eur = 30.0

    # Initiële entry: koop volledig budget op dominant kant
    dom_token  = yes_token if dominant_side == "YES" else no_token
    dom_ask    = yes_ask   if dominant_side == "YES" else no_ask
    dom_shares = round(budget_eur / max(dom_ask, 0.01), 2)

    yes_shares = dom_shares if dominant_side == "YES" else 0.0
    no_shares  = 0.0        if dominant_side == "YES" else dom_shares

    mode = "paper" if force_paper else get_mode()
    market["_bggdsb_yes_shares"]       = yes_shares
    market["_bggdsb_no_shares"]        = no_shares
    market["_bggdsb_dominant_side"]    = dominant_side
    market["_router_bucket"]           = "bggdsb"
    market["_router_conviction_score"] = conv_score

    trade = create_trade_state(coin, market, mode, triggered_by="bggdsb")
    add_active_trade(trade, skip_window_register=True)
    ok = await execute_entry(trade["trade_id"])
    if not ok:
        from .state import remove_active_trade
        remove_active_trade(trade["trade_id"])
        return

    register_window_trade(coin, window_ts)

    # Start window flip task
    initial_yes = budget_eur if dominant_side == "YES" else 0.0
    initial_no  = budget_eur if dominant_side == "NO"  else 0.0
    _bggdsb_tranche_state[window_key] = {
        "coin":          coin,
        "dominant_side": dominant_side,
        "yes_spend":     initial_yes,
        "no_spend":      initial_no,
        "yes_token":     yes_token,
        "no_token":      no_token,
        "window_end_dt": market["window_end"],
        "window_ts":     window_ts,
        "is_paper":      force_paper,
        "window_budget": budget_eur,
        "phase":         "monitoring",
        "hedge_placed":  False,
        "confirm_done":  False,
    }
    task = asyncio.create_task(_bggdsb_window_flip_task(window_key))
    _bggdsb_flip_tasks[window_key] = task
    log.info("bggdsb_entry_and_flip_started", coin=coin,
             dominant_side=dominant_side, budget=budget_eur,
             dom_shares=dom_shares, paper=force_paper)


async def _oracle_coin_paper_gate(coin: str, conviction_score: float, regime: str) -> bool:
    """Return True if Oracle per-coin temperature is below the paper threshold.

    Reads pre-cached temperature from dashboard_state (set by _oracle_per_coin_loop).
    Falls back to False (allow) if Oracle is disabled or temp not yet computed.
    """
    oracle_cfg = CONFIG.get("oracle", {})
    if not oracle_cfg.get("enabled", False):
        return False
    threshold = int(oracle_cfg.get("per_coin_temp_paper_threshold", 35))
    temp_str = None
    try:
        from .db_sync import get_state as _get_state
        temp_str = _get_state(f"oracle_temp_{coin}")
    except Exception:
        pass
    if not temp_str:
        return False
    try:
        temp = int(float(temp_str))
        if temp < threshold:
            log.info("oracle_coin_paper_gate", coin=coin, temp=temp, threshold=threshold)
            return True
    except (ValueError, TypeError):
        pass
    return False


async def _oracle_per_coin_loop() -> None:
    """Compute Oracle temperature per coin every 120s and persist to dashboard_state."""
    while True:
        await asyncio.sleep(120)
        if not CONFIG.get("oracle", {}).get("enabled", False):
            continue
        try:
            from . import oracle as _oracle
            from . import regime as _regime
            from . import signals as _sig
            for coin in COINS:
                if not CONFIG["coins"].get(coin, {}).get("enabled", True):
                    continue
                try:
                    _conv_dir, _conv_score = _sig.get_conviction(coin)
                    _regime_label = _regime.get_current_regime(coin) or "UNKNOWN"
                    snap = await _oracle.get_temperature_snapshot(
                        coin, _conv_score or 0.0, _regime_label
                    )
                    await save_dashboard_state(f"oracle_temp_{coin}", str(snap.get("temperature", 50)))
                except Exception as exc:
                    log.debug("oracle_per_coin_error", coin=coin, error=str(exc))
        except Exception as exc:
            log.debug("oracle_per_coin_loop_error", error=str(exc))


async def _oracle_temperature_loop() -> None:
    """Update Oracle trading temperature in dashboard_state every 60s.

    Uses the first enabled coin's regime/conviction as representative context.
    Runs even when oracle.enabled=False so the widget shows a warming-up state.
    """
    while True:
        await asyncio.sleep(60)
        try:
            if not CONFIG.get("oracle", {}).get("enabled", False):
                continue
            from . import oracle as _oracle
            from . import regime as _regime
            from . import signals as _sig
            # Pick first enabled coin as representative context
            first_coin = next(
                (c for c in COINS if CONFIG["coins"].get(c, {}).get("enabled", True)),
                COINS[0] if COINS else None,
            )
            if not first_coin:
                continue
            _conv_dir, _conv_score = _sig.get_conviction(first_coin)
            _regime_label = _regime.get_current_regime(first_coin) or "UNKNOWN"
            snap = await _oracle.get_temperature_snapshot(
                first_coin, _conv_score or 0.0, _regime_label
            )
            await save_dashboard_state("oracle_trading_temperature", str(snap.get("temperature", 50)))
            await save_dashboard_state("oracle_fear_greed_value", str(snap.get("fear_greed_value", "")))
            await save_dashboard_state("oracle_fear_greed_label", str(snap.get("fear_greed_label", "")))
            await save_dashboard_state("oracle_news_sentiment", str(snap.get("news_sentiment", "")))
            await save_dashboard_state("oracle_polymarket_dir", str(snap.get("polymarket_dir", "")))
            await save_dashboard_state("oracle_track_record", str(snap.get("track_record", "")))
            await save_dashboard_state("oracle_pattern_win_prob", str(snap.get("pattern_win_prob", "")))
        except Exception as exc:
            log.debug("oracle_temperature_loop_error", error=str(exc))


async def _heartbeat_loop() -> None:
    """Write timestamp to DB every 5s so Streamlit can show bot-is-alive indicator."""
    while True:
        try:
            await save_dashboard_state("heartbeat", datetime.now(timezone.utc).isoformat())
        except Exception as e:
            log.warning("heartbeat_write_failed", error=str(e))
        await asyncio.sleep(5)


async def _regime_sync_loop() -> None:
    """Detect regime per coin and save to dashboard_state every 30s for Live tab display.

    Calls detect_regime() with recent DB trades so the in-memory _current_regime dict
    stays current even outside learning/analysis cycles.
    Runs immediately on first iteration so regime is ready before the first trade entry.
    """
    import json as _json
    from . import regime as _regime
    from .logger import get_recent_trades as _get_recent_trades
    while True:
        try:
            recent = await _get_recent_trades(100)
        except Exception:
            recent = []
        for coin in COINS:
            try:
                coin_trades = [t for t in recent if t.get("coin") == coin]
                regime = _regime.detect_regime(coin, coin_trades)
                stats: dict = {
                    "regime": regime,
                    "bias": _regime.get_directional_bias(coin),
                }
                price = _regime.get_asset_price_stats(coin)
                if price:
                    stats["price_position"] = price.get("price_position")
                    stats["range_pct"] = price.get("range_pct")
                await save_dashboard_state(f"regime_{coin}", _json.dumps(stats))
            except Exception as e:
                log.warning("regime_sync_failed", coin=coin, error=str(e))
        await asyncio.sleep(30)


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

            # Portfolio protection: three-layer safety check
            if balance is not None:
                from . import risk as _risk
                await _risk.check_portfolio_protection(balance)
        except Exception as e:
            log.warning("portfolio_sync_failed", error=str(e))
        await asyncio.sleep(30)


async def _learning_tick_loop() -> None:
    """Drive the learning orchestrator — ticks every 10s while in live_learning mode.

    Always runs as a task and gates on the current mode internally, so switching
    into live_learning at runtime (via the dashboard) starts a cycle without
    requiring a full bot restart. The orchestrator is re-fetched each tick so a
    reset_learning_cycle (which nulls the singleton) is picked up immediately
    instead of leaving the loop bound to a stale, closed orchestrator.
    """
    from . import learning as _learning
    while True:
        try:
            if get_mode() == "live_learning":
                orch = _learning.get_orchestrator()
                await orch.start()
                await orch.tick()
        except Exception as e:
            log.error("learning_tick_error", error=str(e))
        await asyncio.sleep(10)


async def _oracle_discord_task() -> None:
    """Start Oracle Discord bot (no-op if token not configured)."""
    import os as _os
    token = (
        _os.environ.get("DISCORD_BOT_TOKEN", "")
        or CONFIG.get("oracle", {}).get("discord_bot_token", "")
    )
    if not token:
        return
    try:
        from .oracle_discord import start_discord_bot
        await start_discord_bot()
    except Exception as exc:
        log.error("oracle_discord_start_error", error=str(exc))


async def _oracle_analysis_loop() -> None:
    """Run Oracle daily analysis at configured UTC hours (default 07:00 + 19:00)."""
    last_hour = -1
    while True:
        try:
            if CONFIG.get("oracle", {}).get("enabled", False):
                now = datetime.now(timezone.utc)
                target_hours = [
                    int(t.split(":")[0])
                    for t in CONFIG.get("oracle", {}).get("daily_analysis_times_utc", ["07:00", "19:00"])
                ]
                if now.hour in target_hours and now.hour != last_hour:
                    last_hour = now.hour
                    from . import oracle as _ora
                    await _ora.run_daily_analysis()
        except Exception as exc:
            log.error("oracle_analysis_loop_error", error=str(exc))
        await asyncio.sleep(60)


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
        asyncio.create_task(_regime_sync_loop()),
        asyncio.create_task(_oracle_temperature_loop()),
        asyncio.create_task(_oracle_per_coin_loop()),
        asyncio.create_task(asset_price_feed.run()),
        asyncio.create_task(_signals.run_trade_poll_loop()),
        asyncio.create_task(_signals.funding_rate_loop()),
        asyncio.create_task(_oracle_discord_task()),
        asyncio.create_task(_oracle_analysis_loop()),
        asyncio.create_task(_whale_tracker.whale_sync_loop()),
    ]

    # Per-coin straddle loops (self-gate op signal_trader mode)
    for coin in COINS:
        if CONFIG["coins"][coin]["enabled"]:
            tasks.append(asyncio.create_task(_coin_loop(coin)))

    # Signal trader loops (self-gate op niet-signal_trader mode)
    from .signal_trader import signal_trader_loop as _st_loop
    for coin in COINS:
        tasks.append(asyncio.create_task(_st_loop(coin)))

    # Always spawn the learning loop; it self-gates on live_learning mode so a
    # runtime mode switch (not just a restart-into-live_learning) activates it.
    tasks.append(asyncio.create_task(_learning_tick_loop()))
    tasks.append(asyncio.create_task(_wg.weighting_guard_loop()))

    await asyncio.gather(*tasks)
