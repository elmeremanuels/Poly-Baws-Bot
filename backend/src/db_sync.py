"""
Synchronous SQLite reads for the Streamlit dashboard.
Uses stdlib sqlite3 (not aiosqlite) so Streamlit can call these directly.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config_loader import CONFIG

_db_path = Path(__file__).parent.parent / CONFIG["logging"]["db_path"]


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ── Trades ────────────────────────────────────────────────────────────────────

def get_recent_trades(limit: int = 20) -> list[dict]:
    if not _db_path.exists():
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_open_trades() -> list[dict]:
    if not _db_path.exists():
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status NOT IN ('closed', 'aborted', 'resolved')"
        ).fetchall()
    return [dict(r) for r in rows]


def get_daily_pnl(coin: str | None = None) -> float:
    if not _db_path.exists():
        return 0.0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _conn() as conn:
        if coin:
            row = conn.execute(
                "SELECT COALESCE(SUM(net_pnl),0) FROM trades WHERE date(created_at)=? AND coin=? AND status='closed'",
                (today, coin),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(net_pnl),0) FROM trades WHERE date(created_at)=? AND status='closed'",
                (today,),
            ).fetchone()
    return float(row[0]) if row else 0.0


def get_today_trade_count(coin: str | None = None) -> int:
    if not _db_path.exists():
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _conn() as conn:
        if coin:
            row = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE date(created_at)=? AND coin=?",
                (today, coin),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE date(created_at)=?", (today,)
            ).fetchone()
    return int(row[0]) if row else 0


# ── State / config ────────────────────────────────────────────────────────────

def get_state(key: str) -> str | None:
    if not _db_path.exists():
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT value FROM dashboard_state WHERE key=?", (key,)
        ).fetchone()
    return row[0] if row else None


def get_bot_heartbeat_age() -> float | None:
    """Returns seconds since last heartbeat, or None if never seen."""
    hb = get_state("heartbeat")
    if not hb:
        return None
    try:
        ts = datetime.fromisoformat(hb).astimezone(timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


# ── Hybrid pending ────────────────────────────────────────────────────────────

def get_hybrid_pending() -> list[dict]:
    if not _db_path.exists():
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM hybrid_pending ORDER BY window_start ASC"
        ).fetchall()
    return [dict(r) for r in rows]


# ── Events / alerts ───────────────────────────────────────────────────────────

def get_recent_events(limit: int = 100) -> list[dict]:
    if not _db_path.exists():
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_events_for_trade(trade_id: str) -> list[dict]:
    if not _db_path.exists():
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE trade_id=? ORDER BY ts ASC", (trade_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_scanner_alerts(limit: int = 5) -> list[dict]:
    if not _db_path.exists():
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE event_type='scanner_alert' ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
