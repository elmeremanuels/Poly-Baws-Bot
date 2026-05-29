import logging
import structlog
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
import aiosqlite
from pathlib import Path

from .config_loader import CONFIG

_db_path = Path(__file__).parent.parent / CONFIG["logging"]["db_path"]
_db_path.parent.mkdir(parents=True, exist_ok=True)

_LOG_LEVEL = (CONFIG.get("logging", {}).get("level") or "INFO").upper()
_LOG_LEVEL_NUM = getattr(logging, _LOG_LEVEL, logging.INFO)

# Mapping van level-naam → integer (zelfde schaal als stdlib logging)
_LEVEL_MAP = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}


def _level_filter(logger, method: str, event_dict: dict) -> dict:
    """Drop events below the configured log level (works with PrintLoggerFactory)."""
    if _LEVEL_MAP.get(method, 20) < _LOG_LEVEL_NUM:
        raise structlog.DropEvent()
    return event_dict


structlog.configure(
    processors=[
        _level_filter,                             # ← filtert debug weg op INFO-instelling
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(_LOG_LEVEL_NUM),
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

log = structlog.get_logger()

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    coin TEXT NOT NULL,
    market_id TEXT,
    condition_id_yes TEXT,
    condition_id_no TEXT,
    question TEXT,
    mode TEXT NOT NULL,
    triggered_by TEXT,
    window_start_ts TEXT,
    window_end_ts TEXT,
    entry_placed_ts TEXT,
    entry_filled_ts TEXT,
    asset_price_at_entry REAL,
    entry_yes_price REAL,
    entry_no_price REAL,
    entry_size INTEGER,
    trigger_hit INTEGER DEFAULT 0,
    trigger_ts TEXT,
    asset_price_at_trigger REAL,
    winner_side TEXT,
    loser_exit_price REAL,
    loser_exit_ts TEXT,
    winner_exit_price REAL,
    winner_exit_ts TEXT,
    winner_exit_reason TEXT,
    fees_paid REAL DEFAULT 0,
    gross_pnl REAL,
    net_pnl REAL,
    status TEXT DEFAULT 'pending',
    notes TEXT,
    peak_bid REAL,
    ratchet_count INTEGER DEFAULT 0,
    time_in_trail_seconds REAL,
    break_even_price REAL,
    actual_winner TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT,
    event_type TEXT NOT NULL,
    coin TEXT,
    data TEXT,
    ts TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT,
    market_id TEXT,
    side TEXT,
    best_bid REAL,
    best_ask REAL,
    snapshot TEXT,
    ts TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dashboard_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    payload TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now')),
    executed_at TEXT
);

CREATE TABLE IF NOT EXISTS hybrid_pending (
    market_id TEXT PRIMARY KEY,
    coin TEXT,
    window_start TEXT,
    question TEXT,
    trade_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fill_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT,
    token_id TEXT,
    side TEXT,
    limit_price REAL,
    filled INTEGER DEFAULT 0,
    fill_price REAL,
    ts TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS learning_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_number INTEGER NOT NULL,
    phase TEXT NOT NULL DEFAULT 'learn',
    phase_started_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at TEXT,
    params_used TEXT,
    claude_analysis TEXT,
    claude_params TEXT,
    confidence_score REAL,
    usdc_at_start REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_trades_created_at ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_trades_analytics ON trades(status, trigger_hit, coin, created_at);
"""


@asynccontextmanager
async def _db():
    """Open an aiosqlite connection with busy_timeout pre-set."""
    async with aiosqlite.connect(_db_path, timeout=10.0) as db:
        await db.execute("PRAGMA busy_timeout=10000")
        yield db


async def init_db() -> None:
    async with aiosqlite.connect(_db_path, timeout=10.0) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA busy_timeout=10000")
        await db.commit()
        await db.executescript(SCHEMA)
        await db.commit()
        # Migration: add trailing-strategy columns if missing
        async with db.execute("PRAGMA table_info(trades)") as cur:
            cols = await cur.fetchall()
        existing = {row[1] for row in cols}
        for col_name, col_type in [
            ("peak_bid", "REAL"),
            ("ratchet_count", "INTEGER DEFAULT 0"),
            ("time_in_trail_seconds", "REAL"),
            ("break_even_price", "REAL"),
            ("actual_winner", "TEXT"),
        ]:
            if col_name not in existing:
                await db.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
        await db.commit()
        # Migration: create fill_history table if missing
        await db.execute(
            "CREATE TABLE IF NOT EXISTS fill_history ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT, token_id TEXT, "
            "side TEXT, limit_price REAL, filled INTEGER DEFAULT 0, fill_price REAL, "
            "ts TEXT DEFAULT (datetime('now')))"
        )
        await db.commit()
        # Migration: new trade columns for learning mode
        for col_name, col_type in [
            ("mid_at_trigger", "REAL"),
            ("spread_at_trigger", "REAL"),
            ("mid_velocity_at_trigger", "REAL"),
            ("yes_depth_at_trigger", "REAL"),
            ("no_depth_at_trigger", "REAL"),
            ("time_since_window_start", "REAL"),
            ("phase", "TEXT DEFAULT 'manual'"),
            ("cycle_id", "INTEGER"),
            ("param_snapshot", "TEXT"),
            ("regime", "TEXT"),
            ("directional_bias", "TEXT"),
            ("price_position", "REAL"),
            ("asset_range_pct", "REAL"),
            ("bias_certainty", "REAL"),
            ("yes_size", "REAL"),
            ("no_size", "REAL"),
            # Bot prediction columns
            ("regime_at_entry", "TEXT"),
            ("bias_direction_at_entry", "TEXT"),
            # Phase 1 signal columns
            ("ofi_at_entry", "REAL"),
            ("funding_rate_at_entry", "REAL"),
            ("liq_proxy_at_entry", "REAL"),
            ("conviction_at_entry", "TEXT"),
            ("conviction_score_at_entry", "REAL"),
            ("ofi_at_trigger", "REAL"),
            ("funding_rate_at_trigger", "REAL"),
            ("liq_proxy_at_trigger", "REAL"),
            ("conviction_at_trigger", "TEXT"),
            ("conviction_score_at_trigger", "REAL"),
            # Migration: Phase 2 A/B test group tracking
            ("ab_group", "TEXT"),
            # Migration: early loser sell during monitoring phase
            ("early_loser_side", "TEXT"),
            ("early_loser_price", "REAL"),
            ("early_loser_ts", "TEXT"),
            ("early_loser_rebought", "INTEGER DEFAULT 0"),
            # Phase 3: directional entry
            ("entry_type", "TEXT DEFAULT 'straddle'"),
            ("directional_side", "TEXT"),
            ("theoretical_price_yes", "REAL"),
            ("theoretical_price_no", "REAL"),
            ("edge_at_entry", "REAL"),
            # Signal trader: market title for Polymarket lookup
            ("question", "TEXT"),
            # Dynamic Position Manager: Take it / Save it
            ("take_it_executed", "INTEGER DEFAULT 0"),
            ("save_it_executed", "INTEGER DEFAULT 0"),
            ("save_it_side", "TEXT"),
            # Auto Router routing decision
            ("router_bucket", "TEXT"),
            ("router_conviction_score", "REAL"),
        ]:
            if col_name not in existing:
                await db.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
        # Migration: create learning_cycles table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS learning_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_number INTEGER NOT NULL,
                phase TEXT NOT NULL DEFAULT 'learn',
                phase_started_at TEXT NOT NULL DEFAULT (datetime('now')),
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                ended_at TEXT,
                params_used TEXT,
                claude_analysis TEXT,
                claude_params TEXT,
                confidence_score REAL,
                usdc_at_start REAL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Migration: add usdc_at_start to existing learning_cycles rows
        async with db.execute("PRAGMA table_info(learning_cycles)") as cur:
            lc_cols = {row[1] for row in await cur.fetchall()}
        if "usdc_at_start" not in lc_cols:
            await db.execute("ALTER TABLE learning_cycles ADD COLUMN usdc_at_start REAL")
        if "claude_prediction" not in lc_cols:
            await db.execute("ALTER TABLE learning_cycles ADD COLUMN claude_prediction TEXT")
        await db.commit()
        # Backfill: yes_size / no_size default to entry_size for pre-bias trades
        await db.execute(
            "UPDATE trades SET yes_size = entry_size WHERE yes_size IS NULL AND entry_size IS NOT NULL"
        )
        await db.execute(
            "UPDATE trades SET no_size = entry_size WHERE no_size IS NULL AND entry_size IS NOT NULL"
        )
        # Backfill: bias_certainty = 0 (no bias) for old trades
        await db.execute(
            "UPDATE trades SET bias_certainty = 0.0 WHERE bias_certainty IS NULL"
        )
        # Backfill: regime_at_entry = 'UNKNOWN' for old trades (explicit vs NULL)
        await db.execute(
            "UPDATE trades SET regime_at_entry = 'UNKNOWN' WHERE regime_at_entry IS NULL"
        )
        await db.commit()
    log.info("database_initialized", path=str(_db_path))


async def write_trade(trade: dict) -> None:
    """Insert or fully replace a trade record.

    created_at is now always included in the record (added to db_fields in
    state.persist_trade), so INSERT OR REPLACE preserves the original creation
    timestamp on every replace instead of resetting it to datetime('now').
    """
    cols = ", ".join(trade.keys())
    placeholders = ", ".join(f":{k}" for k in trade.keys())
    sql = f"INSERT OR REPLACE INTO trades ({cols}) VALUES ({placeholders})"
    async with _db() as db:
        await db.execute(sql, trade)
        await db.commit()


async def update_trade(trade_id: str, updates: dict) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in updates.keys())
    sql = f"UPDATE trades SET {sets} WHERE trade_id = :trade_id"
    async with _db() as db:
        await db.execute(sql, {**updates, "trade_id": trade_id})
        await db.commit()


async def write_event(trade_id: str | None, event_type: str, coin: str | None, data: Any) -> None:
    import json
    async with _db() as db:
        await db.execute(
            "INSERT INTO events (trade_id, event_type, coin, data) VALUES (?, ?, ?, ?)",
            (trade_id, event_type, coin, json.dumps(data) if not isinstance(data, str) else data),
        )
        await db.commit()


async def write_snapshot(trade_id: str, market_id: str, side: str, best_bid: float, best_ask: float, snapshot: dict) -> None:
    import json
    async with _db() as db:
        await db.execute(
            "INSERT INTO orderbook_snapshots (trade_id, market_id, side, best_bid, best_ask, snapshot) VALUES (?, ?, ?, ?, ?, ?)",
            (trade_id, market_id, side, best_bid, best_ask, json.dumps(snapshot)),
        )
        await db.commit()


async def get_trade(trade_id: str) -> dict | None:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_open_trades() -> list[dict]:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM trades WHERE status NOT IN ('closed', 'aborted', 'resolved')"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_recent_trades(limit: int = 20) -> list[dict]:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_daily_pnl(coin: str | None = None) -> float:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with _db() as db:
        if coin:
            async with db.execute(
                "SELECT COALESCE(SUM(net_pnl), 0) FROM trades WHERE date(created_at) = ? AND coin = ? AND status IN ('closed', 'resolved')",
                (today, coin),
            ) as cursor:
                row = await cursor.fetchone()
        else:
            async with db.execute(
                "SELECT COALESCE(SUM(net_pnl), 0) FROM trades WHERE date(created_at) = ? AND status IN ('closed', 'resolved')",
                (today,),
            ) as cursor:
                row = await cursor.fetchone()
        return row[0] if row else 0.0


async def get_mode_daily_pnl(mode: str) -> float:
    """Sum today's net P&L for trades of a specific mode (e.g. 'signal_trader')."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with _db() as db:
        async with db.execute(
            "SELECT COALESCE(SUM(net_pnl), 0) FROM trades "
            "WHERE date(created_at) = ? AND mode = ? AND status IN ('closed', 'resolved')",
            (today, mode),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0.0


async def get_today_trade_count(coin: str | None = None) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with _db() as db:
        if coin:
            async with db.execute(
                "SELECT COUNT(*) FROM trades WHERE date(created_at) = ? AND coin = ?",
                (today, coin),
            ) as cursor:
                row = await cursor.fetchone()
        else:
            async with db.execute(
                "SELECT COUNT(*) FROM trades WHERE date(created_at) = ?",
                (today,),
            ) as cursor:
                row = await cursor.fetchone()
        return row[0] if row else 0


async def save_dashboard_state(key: str, value: str) -> None:
    async with _db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO dashboard_state (key, value, updated_at) VALUES (?, ?, datetime('now'))",
            (key, value),
        )
        await db.commit()


async def load_dashboard_state(key: str) -> str | None:
    async with _db() as db:
        async with db.execute("SELECT value FROM dashboard_state WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def reset_paper_data() -> None:
    """Delete all trades, events and snapshots — used when resetting paper trading state."""
    async with _db() as db:
        await db.execute("DELETE FROM trades")
        await db.execute("DELETE FROM events")
        await db.execute("DELETE FROM orderbook_snapshots")
        await db.commit()
    log.info("paper_data_reset")


async def get_events_for_trade(trade_id: str) -> list[dict]:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM events WHERE trade_id = ? ORDER BY ts ASC", (trade_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_scanner_alerts(limit: int = 10) -> list[dict]:
    """Return most recent scanner alert events (slug mismatches, no-markets warnings)."""
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM events WHERE event_type = 'scanner_alert' ORDER BY ts DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
