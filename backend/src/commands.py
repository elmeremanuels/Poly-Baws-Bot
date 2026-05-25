"""
Command queue: Streamlit writes commands via sqlite3 (sync),
bot executes them via aiosqlite (async, polling every 1s).
"""
import asyncio
import json
import sqlite3
import time
from pathlib import Path

import aiosqlite

from .config_loader import CONFIG
from .logger import log, save_dashboard_state, _db_path


# ── Sync write (called from Streamlit) ───────────────────────────────────────

def _sync_write(sql: str, params: tuple = ()) -> None:
    """
    Execute a single write statement with BEGIN IMMEDIATE for reliable WAL locking.
    Retries up to 8 times with exponential backoff before raising.
    """
    for attempt in range(8):
        conn = sqlite3.connect(str(_db_path), isolation_level=None, timeout=30)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(sql, params)
            conn.execute("COMMIT")
            return
        except sqlite3.OperationalError as exc:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            if "locked" not in str(exc) or attempt == 7:
                raise
            time.sleep(0.05 * (2 ** attempt))  # 50 100 200 400 800 1600 3200ms
        finally:
            conn.close()


def write_command(command: str, payload: dict | None = None) -> None:
    _sync_write(
        "INSERT INTO commands (command, payload) VALUES (?, ?)",
        (command, json.dumps(payload or {})),
    )


def write_hybrid_pending(market_id: str, coin: str, window_start: str, question: str, trade_id: str) -> None:
    _sync_write(
        "INSERT OR REPLACE INTO hybrid_pending (market_id, coin, window_start, question, trade_id) VALUES (?, ?, ?, ?, ?)",
        (market_id, coin, window_start, question, trade_id),
    )


def delete_hybrid_pending(market_id: str) -> None:
    _sync_write(
        "DELETE FROM hybrid_pending WHERE market_id = ?",
        (market_id,),
    )


# ── Async execution (called from bot process) ─────────────────────────────────

async def command_poll_loop() -> None:
    """Runs inside the bot's asyncio loop; polls for and executes pending commands."""
    while True:
        try:
            await _execute_pending()
        except Exception as e:
            log.error("command_poll_error", error=str(e))
        await asyncio.sleep(1.0)


async def _execute_pending() -> None:
    async with aiosqlite.connect(str(_db_path), timeout=10.0) as db:
        await db.execute("PRAGMA busy_timeout=30000")
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM commands WHERE status = 'pending' ORDER BY id ASC LIMIT 20"
        ) as cur:
            rows = await cur.fetchall()

        for row in rows:
            cmd = row["command"]
            payload = json.loads(row["payload"] or "{}")
            try:
                await _run_command(cmd, payload)
                await db.execute(
                    "UPDATE commands SET status='done', executed_at=datetime('now') WHERE id=?",
                    (row["id"],),
                )
                await db.commit()
                log.info("command_executed", command=cmd, id=row["id"])
            except Exception as e:
                log.error("command_error", command=cmd, error=str(e))
                await db.execute(
                    "UPDATE commands SET status='error', executed_at=datetime('now') WHERE id=?",
                    (row["id"],),
                )
                await db.commit()


async def _run_command(command: str, payload: dict) -> None:
    from . import risk
    from .state import set_mode, reset_state, get_active_trades, is_paper_mode
    from .logger import reset_paper_data

    if command == "force_close_trade":
        from . import orders as _orders, paper_trader as _pt
        from .state import update_trade_field, remove_active_trade, persist_trade
        from .monitor import stop_monitoring
        from .triggers import _close_trade

        trade_id = payload.get("trade_id")
        trade = get_active_trades().get(trade_id)
        if not trade:
            log.warning("force_close_not_found", trade_id=trade_id)
            return

        await stop_monitoring(trade_id)

        paper = is_paper_mode()
        size = trade.get("entry_size") or 2
        yes_token = trade.get("condition_id_yes")
        no_token = trade.get("condition_id_no")
        winner_side = trade.get("winner_side")
        status = trade.get("status", "")
        fill_price = None

        if status == "monitoring":
            # Trigger not yet hit — sell both legs
            if paper:
                yr = await _pt.simulate_market_sell(yes_token, size)
                nr = await _pt.simulate_market_sell(no_token, size)
                fill_price = yr.get("fill_price")
                update_trade_field(trade_id, "loser_exit_price", nr.get("fill_price"))
            else:
                await _orders.cancel_order(trade.get("yes_order_id") or "")
                await _orders.cancel_order(trade.get("no_order_id") or "")
                await _orders.place_market_order(yes_token, "SELL", size)
                await _orders.place_market_order(no_token, "SELL", size)
        else:
            # Trigger hit — only winner remains
            winner_token = yes_token if winner_side == "YES" else no_token
            if paper:
                r = await _pt.simulate_market_sell(winner_token, size)
                fill_price = r.get("fill_price")
            else:
                await _orders.cancel_all_orders()
                await _orders.place_market_order(winner_token, "SELL", size)

        await _close_trade(trade_id, fill_price, "force_closed_by_user", None)
        log.info("force_close_done", trade_id=trade_id, status=status)

    elif command == "set_mode":
        set_mode(payload["mode"])
        await save_dashboard_state("mode", payload["mode"])

    elif command == "kill":
        risk.kill("dashboard_user")

    elif command == "reset_kill":
        risk.reset_kill()

    elif command == "reset_paper":
        reset_state()
        await reset_paper_data()

    elif command == "trigger_hybrid":
        from . import bot as bot_module
        await bot_module.trigger_hybrid_entry(payload["market_id"])

    elif command == "set_coin_config":
        coin = payload["coin"]
        if "enabled" in payload:
            CONFIG["coins"][coin]["enabled"] = payload["enabled"]
        if "max_parallel" in payload:
            CONFIG["coins"][coin]["max_parallel_positions"] = int(payload["max_parallel"])
        await save_dashboard_state(f"coin_{coin}_enabled", str(CONFIG["coins"][coin]["enabled"]))
        await save_dashboard_state(f"coin_{coin}_max", str(CONFIG["coins"][coin]["max_parallel_positions"]))

    elif command == "run_pattern_match":
        from . import pattern_matcher as _pm
        await _pm.run_pattern_backtest()
        log.info("pattern_match_forced")

    elif command == "coin_guard_enable":
        coin = payload["coin"]
        from . import coin_guard as _cg
        await _cg.enable_coin(coin)
        CONFIG["coins"][coin]["enabled"] = True
        await save_dashboard_state(f"coin_{coin}_enabled", "True")
        log.info("coin_guard_enabled_by_command", coin=coin)

    elif command == "coin_guard_paper_gate":
        coin = payload["coin"]
        n = int(payload.get("n", 5))
        from . import coin_guard as _cg
        await _cg.enable_coin(coin)
        _cg.start_paper_gate(coin, n)
        CONFIG["coins"][coin]["enabled"] = True
        await save_dashboard_state(f"coin_{coin}_enabled", "True")
        log.info("coin_guard_paper_gate_started", coin=coin, n=n)

    elif command == "set_trade_size":
        eur = float(payload["trade_size_eur"])
        CONFIG["trading"]["trade_size_eur"] = eur
        await save_dashboard_state("trade_size_eur", str(eur))
        log.info("trade_size_updated", eur=eur)

    elif command == "pause_learning":
        from . import learning as _learning
        _learning.get_orchestrator()
        log.info("learning_paused_by_user")
        # Trading is paused via the existing kill mechanism

    elif command == "force_next_phase":
        from . import learning as _learning
        orch = _learning.get_orchestrator()
        phase = orch._phase
        next_map = {"learn": "analyze", "analyze": "deploy", "deploy": "analyze", "validate": "learn"}
        next_phase = next_map.get(phase, "learn")
        await orch._transition_to(next_phase)
        log.info("learning_phase_forced", to=next_phase)

    elif command == "force_deploy":
        from . import learning as _learning
        orch = _learning.get_orchestrator()
        await orch._transition_to("deploy")
        log.info("learning_force_deploy")

    elif command == "reset_learning_cycle":
        from . import learning as _learning
        from .logger import _db as _adb
        async with _adb() as db:
            await db.execute(
                "UPDATE learning_cycles SET ended_at=datetime('now') WHERE ended_at IS NULL"
            )
            await db.commit()
        _learning._orchestrator_instance = None
        log.info("learning_cycle_reset")

    elif command == "toggle_learned_params":
        from . import learning as _learning
        enabled = payload.get("enabled", False)
        if enabled:
            ok = await _learning.load_and_apply_latest_params()
            if not ok:
                log.warning("toggle_learned_params_no_data",
                            hint="Run live_learning first to generate a completed cycle.")
        else:
            _learning.restore_learned_params()
        await save_dashboard_state("apply_learnings", "true" if enabled else "false")

    elif command == "clear_orphaned_positions":
        from . import orders as _orders
        from .state import get_active_trades

        active_tokens: set[str] = set()
        for t in get_active_trades().values():
            if t.get("condition_id_yes"):
                active_tokens.add(t["condition_id_yes"])
            if t.get("condition_id_no"):
                active_tokens.add(t["condition_id_no"])

        positions = await _orders.get_open_positions()
        cleared = 0
        for pos in positions:
            token_id = pos.get("asset") or pos.get("token_id") or pos.get("market") or ""
            size = float(pos.get("size") or pos.get("amount") or 0)
            if not token_id or size <= 0:
                continue
            if token_id in active_tokens:
                log.info("clear_orphaned_skip_active", token_id=token_id[:16])
                continue
            log.info("clear_orphaned_selling", token_id=token_id[:16], size=size)
            await _orders.place_market_order(token_id, "SELL", size)
            cleared += 1

        log.info("clear_orphaned_done", cleared=cleared, active_protected=len(active_tokens))

    elif command == "reset_portfolio_start":
        from .logger import load_dashboard_state
        current = await load_dashboard_state("portfolio_value")
        if current:
            await save_dashboard_state("portfolio_start_usdc", current)
            log.info("portfolio_start_reset", value=current)

    else:
        log.warning("unknown_command", command=command)
