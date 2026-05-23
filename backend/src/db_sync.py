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
    conn = sqlite3.connect(str(_db_path), isolation_level=None, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
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
                "SELECT COALESCE(SUM(net_pnl),0) FROM trades WHERE date(created_at)=? AND coin=? AND status IN ('closed','resolved')",
                (today, coin),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(net_pnl),0) FROM trades WHERE date(created_at)=? AND status IN ('closed','resolved')",
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


def get_scanner_state(coin: str) -> dict:
    """Return latest scanner info for a coin: {count, next_start, next_end}."""
    raw = get_state(f"scanner_{coin}")
    if not raw:
        return {"count": 0}
    try:
        return json.loads(raw)
    except Exception:
        return {"count": 0}


def get_scanner_alerts(limit: int = 5) -> list[dict]:
    if not _db_path.exists():
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE event_type='scanner_alert' ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Analytics ─────────────────────────────────────────────────────────────────

def get_analytics_trades(coin: str | None = None, days: int | None = None) -> list[dict]:
    """All trades, optionally filtered by coin and date range. No trigger/status filter."""
    if not _db_path.exists():
        return []
    conditions: list[str] = []
    params: list = []
    if coin:
        conditions.append("coin = ?")
        params.append(coin)
    if days:
        conditions.append("created_at >= datetime('now', ?)")
        params.append(f"-{days} days")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY created_at ASC",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_exit_reason_stats(coin: str | None = None, days: int | None = None) -> list[dict]:
    """Aggregate stats grouped by winner_exit_reason."""
    if not _db_path.exists():
        return []
    conditions = ["status IN ('closed','resolved')", "trigger_hit = 1"]
    params: list = []
    if coin:
        conditions.append("coin = ?")
        params.append(coin)
    if days:
        conditions.append("created_at >= datetime('now', ?)")
        params.append(f"-{days} days")
    where = " AND ".join(conditions)
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT
                winner_exit_reason,
                COUNT(*) as count,
                ROUND(AVG(net_pnl), 4) as avg_pnl,
                ROUND(SUM(net_pnl), 4) as total_pnl,
                ROUND(AVG(peak_bid), 4) as avg_peak_bid,
                ROUND(AVG(ratchet_count), 1) as avg_ratchets,
                ROUND(AVG(time_in_trail_seconds), 1) as avg_trail_time
            FROM trades
            WHERE {where}
            GROUP BY winner_exit_reason
            ORDER BY count DESC""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_coin_comparison(days: int | None = None) -> list[dict]:
    """Per-coin aggregated stats for closed triggered trades."""
    if not _db_path.exists():
        return []
    conditions = ["status IN ('closed','resolved')", "trigger_hit = 1"]
    params: list = []
    if days:
        conditions.append("created_at >= datetime('now', ?)")
        params.append(f"-{days} days")
    where = " AND ".join(conditions)
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT
                coin,
                COUNT(*) as trades,
                ROUND(SUM(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END) / COUNT(*) * 100, 1) as win_rate,
                ROUND(SUM(net_pnl), 4) as total_pnl,
                ROUND(AVG(net_pnl), 4) as avg_pnl,
                ROUND(AVG(peak_bid), 4) as avg_peak_bid,
                ROUND(AVG(time_in_trail_seconds), 1) as avg_trail_time,
                ROUND(AVG(fees_paid), 4) as avg_fees
            FROM trades
            WHERE {where}
            GROUP BY coin
            ORDER BY total_pnl DESC""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_hourly_pnl(coin: str | None = None, days: int | None = None) -> list[dict]:
    """Average P&L by hour of day (UTC)."""
    if not _db_path.exists():
        return []
    conditions = ["status IN ('closed','resolved')", "trigger_hit = 1"]
    params: list = []
    if coin:
        conditions.append("coin = ?")
        params.append(coin)
    if days:
        conditions.append("created_at >= datetime('now', ?)")
        params.append(f"-{days} days")
    where = " AND ".join(conditions)
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT
                CAST(strftime('%H', created_at) AS INTEGER) as hour_utc,
                COUNT(*) as trades,
                ROUND(AVG(net_pnl), 4) as avg_pnl,
                ROUND(SUM(net_pnl), 4) as total_pnl
            FROM trades
            WHERE {where}
            GROUP BY hour_utc
            ORDER BY hour_utc""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


# ── Learning cycles ───────────────────────────────────────────────────────────

def get_current_cycle() -> dict | None:
    """Return the active (not ended) learning cycle, or None."""
    if not _db_path.exists():
        return None
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT * FROM learning_cycles WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def get_latest_completed_cycle() -> dict | None:
    """Return the most recent completed cycle that has claude_params, for the apply-learnings toggle."""
    if not _db_path.exists():
        return None
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT * FROM learning_cycles WHERE ended_at IS NOT NULL AND claude_params IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def get_phase_stats(cycle_id: int, phase: str) -> dict:
    """Aggregated stats for a specific phase of a cycle."""
    if not _db_path.exists():
        return {}
    with _conn() as conn:
        row = conn.execute(
            """SELECT
                COUNT(*) as trades,
                SUM(CASE WHEN trigger_hit=1 THEN 1 ELSE 0 END) as triggered,
                ROUND(AVG(CASE WHEN trigger_hit=1 AND net_pnl IS NOT NULL
                    THEN net_pnl END), 4) as avg_pnl,
                ROUND(SUM(CASE WHEN net_pnl IS NOT NULL THEN net_pnl ELSE 0 END), 4) as total_pnl,
                ROUND(SUM(CASE WHEN trigger_hit=1 AND net_pnl > 0 THEN 1.0 ELSE 0.0 END)
                    / NULLIF(SUM(CASE WHEN trigger_hit=1 THEN 1 ELSE 0 END), 0) * 100, 1) as win_rate
            FROM trades WHERE cycle_id=? AND phase=?""",
            (cycle_id, phase),
        ).fetchone()
    return dict(row) if row else {}


def get_cycle_stats(cycle_id: int) -> dict:
    """Full per-coin stats for a learning cycle, used for Claude analysis prompt."""
    if not _db_path.exists():
        return {}
    with _conn() as conn:
        rows = conn.execute(
            """SELECT coin,
                COUNT(*) as trades,
                SUM(CASE WHEN trigger_hit=1 THEN 1 ELSE 0 END) as triggered,
                ROUND(SUM(CASE WHEN trigger_hit=1 AND net_pnl > 0 THEN 1.0 ELSE 0.0 END)
                    / NULLIF(SUM(CASE WHEN trigger_hit=1 THEN 1 ELSE 0 END), 0) * 100, 1) as win_rate,
                ROUND(AVG(CASE WHEN trigger_hit=1 AND net_pnl IS NOT NULL
                    THEN net_pnl END), 4) as avg_pnl,
                ROUND(AVG(CASE WHEN trigger_hit=1 THEN peak_bid END), 4) as avg_peak_bid,
                ROUND(AVG(CASE WHEN trigger_hit=1 THEN mid_at_trigger END), 4) as avg_mid_at_trigger,
                ROUND(AVG(CASE WHEN trigger_hit=1 THEN spread_at_trigger END), 4) as avg_spread_at_trigger,
                ROUND(AVG(CASE WHEN trigger_hit=1 THEN time_in_trail_seconds END), 1) as avg_trail_time,
                ROUND(AVG(CASE WHEN trigger_hit=1 THEN ratchet_count END), 1) as avg_ratchets,
                ROUND(AVG(entry_yes_price + entry_no_price), 4) as avg_entry_cost,
                ROUND(SUM(CASE WHEN winner_exit_reason='peg_cross' AND trigger_hit=1
                    THEN 1.0 ELSE 0.0 END)
                    / NULLIF(SUM(CASE WHEN trigger_hit=1 THEN 1 ELSE 0 END), 0), 2) as peg_cross_rate,
                ROUND(SUM(CASE WHEN winner_exit_reason='limit_filled' AND trigger_hit=1
                    THEN 1.0 ELSE 0.0 END)
                    / NULLIF(SUM(CASE WHEN trigger_hit=1 THEN 1 ELSE 0 END), 0), 2) as limit_filled_rate
            FROM trades WHERE cycle_id=?
            GROUP BY coin""",
            (cycle_id,),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*), ROUND(AVG(net_pnl),4) FROM trades WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()
    per_coin = {dict(r)["coin"]: {k: v for k, v in dict(r).items() if k != "coin"} for r in rows}
    return {
        "per_coin": per_coin,
        "total_trades": total[0] if total else 0,
        "overall_avg_pnl": total[1] if total else None,
    }


def get_cycle_trades(cycle_id: int, limit: int = 50) -> list[dict]:
    """Recent trades from a cycle for Claude's detailed log."""
    if not _db_path.exists():
        return []
    with _conn() as conn:
        rows = conn.execute(
            """SELECT coin, mode, phase, trigger_hit, winner_exit_reason,
                mid_at_trigger, spread_at_trigger, mid_velocity_at_trigger,
                peak_bid, ratchet_count, time_in_trail_seconds,
                entry_yes_price, entry_no_price, net_pnl, fees_paid
               FROM trades WHERE cycle_id=? ORDER BY created_at DESC LIMIT ?""",
            (cycle_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
