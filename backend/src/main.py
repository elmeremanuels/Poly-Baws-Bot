"""Bot process entry point — no HTTP server, no WebSocket push."""
import asyncio
import argparse
import sys

from .logger import init_db, log
from .state import recover_state, set_mode
from .logger import load_dashboard_state
from .config_loader import CONFIG
from . import risk
from . import bot as bot_module
from .commands import command_poll_loop


async def _close_stale_recovered_trades() -> None:
    """
    After recover_state(), clean up trades that were left open when the bot was last down.
    Trades whose window has already ended are closed/resolved immediately so they don't
    persist as zombie entries in the Live tab.
    """
    from datetime import datetime, timezone
    from .state import get_active_trades, update_trade_field, remove_active_trade, persist_trade
    from .triggers import _handle_resolution, _close_trade

    from .config_loader import CONFIG as _CFG
    now = datetime.now(timezone.utc)
    # Max plausible size: even at the cheapest entry (30¢), 10× trade_size_eur is the limit.
    _trade_size_eur = _CFG["trading"].get("trade_size_eur", 1.0)
    _max_sane_size = round(_trade_size_eur / 0.10 * 2, 2)  # generous upper bound

    for trade_id, trade in list(get_active_trades().items()):
        status = trade.get("status", "")
        if status not in ("monitoring", "exiting", "entry_placed", "pending"):
            continue

        # Abort recovered trades with suspiciously large entry_size (from old/misconfigured runs).
        entry_size = trade.get("entry_size") or 0
        if entry_size > _max_sane_size:
            log.warning("recovery_aborted_size_mismatch",
                        trade_id=trade_id, entry_size=entry_size, max_sane=_max_sane_size)
            try:
                update_trade_field(trade_id, "status", "aborted")
                update_trade_field(trade_id, "notes", f"recovery_size_mismatch:{entry_size}")
                await persist_trade(trade_id)
            except Exception as e:
                log.error("recovery_cleanup_failed", trade_id=trade_id, error=str(e))
            finally:
                remove_active_trade(trade_id)
            continue

        window_end = trade.get("window_end_ts")
        if not window_end:
            try:
                update_trade_field(trade_id, "status", "aborted")
                update_trade_field(trade_id, "notes", "recovery_no_window_info")
                await persist_trade(trade_id)
                remove_active_trade(trade_id)
                log.warning("recovery_aborted_no_window", trade_id=trade_id, status=status)
            except Exception as e:
                log.error("recovery_cleanup_failed", trade_id=trade_id, error=str(e))
                remove_active_trade(trade_id)
            continue

        window_end_dt = datetime.fromisoformat(window_end).astimezone(timezone.utc)
        if now < window_end_dt:
            # Window still open — resume the appropriate loop instead of leaving it as a zombie.
            if status == "monitoring":
                from .monitor import start_monitoring as _sm
                from .triggers import on_trigger as _on_trigger
                log.info("recovery_resuming_monitoring", trade_id=trade_id,
                         seconds_left=round((window_end_dt - now).total_seconds()))
                await _sm(trade_id, lambda tid, w, p: _on_trigger(tid, w, p, None))
            elif status == "exiting":
                # Exit order ID was lost on restart; settle as resolution (both tokens held → $1).
                log.info("recovery_closing_exiting_open_window", trade_id=trade_id)
                await _close_trade(trade_id, 1.0, "resolution_recovery_open_window", None)
            else:
                # entry_placed / pending — entry was mid-flight when bot died; abort.
                update_trade_field(trade_id, "status", "aborted")
                update_trade_field(trade_id, "notes", "recovery_entry_incomplete")
                await persist_trade(trade_id)
                remove_active_trade(trade_id)
                log.warning("recovery_aborted_incomplete_entry", trade_id=trade_id, status=status)
            continue

        log.info("recovery_closing_stale_trade", trade_id=trade_id, status=status,
                 expired_seconds_ago=round((now - window_end_dt).total_seconds()))

        try:
            if status == "monitoring":
                await _handle_resolution(trade_id, None)
            elif status == "exiting":
                await _close_trade(trade_id, 1.0, "resolution_recovery", None)
            else:
                update_trade_field(trade_id, "status", "aborted")
                update_trade_field(trade_id, "notes", "recovery_expired_before_fill")
                await persist_trade(trade_id)
                remove_active_trade(trade_id)
        except Exception as e:
            log.error("recovery_cleanup_failed", trade_id=trade_id, status=status, error=str(e))
            remove_active_trade(trade_id)  # always remove from memory even if DB write fails


async def _run() -> None:
    log.info("startup_begin")
    await init_db()

    if risk.is_killed():
        log.warning("kill_flag_present_on_startup_trades_paused")
        # Do not abort — command_poll_loop must run so the dashboard can reset the flag

    await recover_state()
    await _close_stale_recovered_trades()

    saved_mode = await load_dashboard_state("mode")
    if saved_mode:
        try:
            set_mode(saved_mode)
        except ValueError:
            pass

    saved_trade_size = await load_dashboard_state("trade_size_eur")
    if saved_trade_size:
        try:
            saved_val = float(saved_trade_size)
            cfg_val = CONFIG["trading"].get("trade_size_eur", 1.0)
            # Cap saved value to 3× the config value to catch stale/accidental overrides.
            if saved_val > cfg_val * 3:
                log.warning("startup_trade_size_clamped",
                            saved=saved_val, config=cfg_val, using=cfg_val)
            else:
                CONFIG["trading"]["trade_size_eur"] = saved_val
        except ValueError:
            pass

    for coin in list(CONFIG["coins"].keys()):
        saved_max = await load_dashboard_state(f"coin_{coin}_max")
        if saved_max:
            try:
                CONFIG["coins"][coin]["max_parallel_positions"] = int(saved_max)
            except ValueError:
                pass
        saved_enabled = await load_dashboard_state(f"coin_{coin}_enabled")
        if saved_enabled is not None:
            CONFIG["coins"][coin]["enabled"] = saved_enabled.lower() == "true"

    if await load_dashboard_state("apply_learnings") == "true":
        from . import learning as _learning
        ok = await _learning.load_and_apply_latest_params()
        if not ok:
            log.warning("startup_apply_learnings_no_data")

    # For live modes, verify Polymarket credentials up front so wallet_info /
    # api_key_auth_failed surface immediately instead of on the first trade.
    from .state import get_mode as _get_mode
    if _get_mode().startswith("live"):
        from . import orders as _orders
        ok = await _orders.check_credentials()
        if not ok:
            log.warning("startup_credentials_invalid_live_orders_will_fail")

    log.info("startup_complete", mode=risk.get_kill_reason() or "ok")

    await asyncio.gather(
        bot_module.run_bot(),
        command_poll_loop(),
    )


def cli() -> None:
    parser = argparse.ArgumentParser(description="Poly-Baws-Bot")
    parser.add_argument("--kill", action="store_true")
    parser.add_argument("--reset-kill", action="store_true")
    args = parser.parse_args()

    if args.kill:
        risk.kill("cli")
        print("Kill switch activated.")
        sys.exit(0)
    if args.reset_kill:
        risk.reset_kill()
        print("Kill switch reset.")
        sys.exit(0)

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
