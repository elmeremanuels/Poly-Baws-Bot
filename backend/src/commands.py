"""
Command queue: Streamlit writes commands via sqlite3 (sync),
bot executes them via aiosqlite (async, polling every 1s).
"""
import asyncio
import json
import sqlite3
from pathlib import Path

import aiosqlite

from .config_loader import CONFIG
from .logger import log, save_dashboard_state, _db_path


# ── Sync write (called from Streamlit) ───────────────────────────────────────

def write_command(command: str, payload: dict | None = None) -> None:
    with sqlite3.connect(_db_path) as conn:
        conn.execute(
            "INSERT INTO commands (command, payload) VALUES (?, ?)",
            (command, json.dumps(payload or {})),
        )
        conn.commit()


def write_hybrid_pending(market_id: str, coin: str, window_start: str, question: str, trade_id: str) -> None:
    with sqlite3.connect(_db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO hybrid_pending (market_id, coin, window_start, question, trade_id) VALUES (?, ?, ?, ?, ?)",
            (market_id, coin, window_start, question, trade_id),
        )
        conn.commit()


def delete_hybrid_pending(market_id: str) -> None:
    with sqlite3.connect(_db_path) as conn:
        conn.execute("DELETE FROM hybrid_pending WHERE market_id = ?", (market_id,))
        conn.commit()


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
    async with aiosqlite.connect(_db_path) as db:
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

    else:
        log.warning("unknown_command", command=command)
