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
    async with aiosqlite.connect(str(_db_path)) as db:
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
    from .state import set_mode

    if command == "set_mode":
        set_mode(payload["mode"])
        await save_dashboard_state("mode", payload["mode"])

    elif command == "kill":
        risk.kill("dashboard_user")

    elif command == "reset_kill":
        risk.reset_kill()

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

    elif command == "set_trade_size_eur":
        eur = float(payload["eur"])
        CONFIG["trading"]["entry_size_eur"] = eur
        price = CONFIG["trading"]["entry_price_target"]
        CONFIG["trading"]["entry_size_shares"] = max(1, int(eur / price))
        await save_dashboard_state("trade_size_eur", str(eur))
        log.info("trade_size_updated", eur=eur, shares=CONFIG["trading"]["entry_size_shares"])

    else:
        log.warning("unknown_command", command=command)
