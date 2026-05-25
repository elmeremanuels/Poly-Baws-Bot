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
_indexes_created = False


def _conn() -> sqlite3.Connection:
    global _indexes_created
    conn = sqlite3.connect(str(_db_path), isolation_level=None, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    if not _indexes_created and _db_path.exists():
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_created_at ON trades(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_analytics ON trades(status, trigger_hit, coin, created_at)")
        _indexes_created = True
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

def get_analytics_trades(coin: str | None = None, days: int | None = None,
                         only_today: bool = False) -> list[dict]:
    """All trades, optionally filtered by coin and date range. No trigger/status filter."""
    if not _db_path.exists():
        return []
    conditions: list[str] = []
    params: list = []
    if coin:
        conditions.append("coin = ?")
        params.append(coin)
    if only_today:
        conditions.append("date(created_at) = date('now')")
    elif days:
        conditions.append("created_at >= datetime('now', ?)")
        params.append(f"-{days} days")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY created_at ASC",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_exit_reason_stats(coin: str | None = None, days: int | None = None,
                          only_today: bool = False) -> list[dict]:
    """Aggregate stats grouped by winner_exit_reason."""
    if not _db_path.exists():
        return []
    conditions = ["status IN ('closed','resolved')", "trigger_hit = 1"]
    params: list = []
    if coin:
        conditions.append("coin = ?")
        params.append(coin)
    if only_today:
        conditions.append("date(created_at) = date('now')")
    elif days:
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


def get_coin_comparison(days: int | None = None, only_today: bool = False) -> list[dict]:
    """Per-coin aggregated stats for closed triggered trades."""
    if not _db_path.exists():
        return []
    conditions = ["status IN ('closed','resolved')", "trigger_hit = 1"]
    params: list = []
    if only_today:
        conditions.append("date(created_at) = date('now')")
    elif days:
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


def get_hourly_pnl(coin: str | None = None, days: int | None = None,
                   only_today: bool = False) -> list[dict]:
    """Average P&L by hour of day (UTC)."""
    if not _db_path.exists():
        return []
    conditions = ["status IN ('closed','resolved')", "trigger_hit = 1"]
    params: list = []
    if coin:
        conditions.append("coin = ?")
        params.append(coin)
    if only_today:
        conditions.append("date(created_at) = date('now')")
    elif days:
        conditions.append("created_at >= datetime('now', ?)")
        params.append(f"-{days} days")
    where = " AND ".join(conditions)
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT
                CAST(strftime('%H', created_at) AS INTEGER) as hour_utc,
                COUNT(*) as trades,
                ROUND(AVG(net_pnl), 4) as avg_pnl,
                ROUND(SUM(net_pnl), 4) as total_pnl,
                ROUND(
                    SUM(CASE WHEN actual_winner = winner_side AND trigger_hit = 1 THEN 1.0 ELSE 0.0 END)
                    / NULLIF(SUM(CASE WHEN trigger_hit = 1 AND actual_winner IS NOT NULL THEN 1 ELSE 0 END), 0)
                    * 100, 1
                ) as direction_accuracy
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
                    / NULLIF(SUM(CASE WHEN trigger_hit=1 THEN 1 ELSE 0 END), 0), 2) as limit_filled_rate,
                ROUND(AVG(CASE WHEN trigger_hit=1 THEN loser_exit_price END), 4) as avg_loser_exit_price,
                ROUND(AVG(CASE WHEN trigger_hit=1 THEN winner_exit_price END), 4) as avg_winner_exit_price,
                ROUND(AVG(CASE WHEN trigger_hit=1 THEN break_even_price END), 4) as avg_break_even_price
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


def get_latest_snapshot_for_trade(trade_id: str) -> dict | None:
    """Return latest orderbook snapshot for a trade, with YES/NO mid prices."""
    if not _db_path.exists():
        return None
    import json as _json
    with _conn() as conn:
        row = conn.execute(
            "SELECT snapshot, best_bid, best_ask, ts FROM orderbook_snapshots "
            "WHERE trade_id=? ORDER BY id DESC LIMIT 1",
            (trade_id,),
        ).fetchone()
    if not row:
        return None
    try:
        snap = _json.loads(row[0] or "{}")
        ts = row[3]
        age = None
        if ts:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - dt).total_seconds()
        return {**snap, "best_bid": row[1], "best_ask": row[2], "age_seconds": age}
    except Exception:
        return None


def get_latest_manual_analysis() -> dict | None:
    """Return the most recent manual Claude analysis (phase='manual') from analytics tab."""
    if not _db_path.exists():
        return None
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT * FROM learning_cycles WHERE phase = 'manual' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def save_manual_analysis_to_db(result: dict) -> None:
    """Save a manual Claude analysis as a learning cycle record so apply-learnings can use it."""
    if not _db_path.exists():
        return
    import json
    with _conn() as conn:
        row = conn.execute("SELECT COALESCE(MAX(cycle_number), 0) FROM learning_cycles").fetchone()
        next_cycle = (row[0] if row else 0) + 1
        conn.execute(
            """INSERT INTO learning_cycles
               (cycle_number, phase, phase_started_at, started_at, ended_at,
                claude_analysis, claude_params, confidence_score)
               VALUES (?, 'manual', datetime('now'), datetime('now'), datetime('now'), ?, ?, ?)""",
            (
                next_cycle,
                result.get("reasoning", ""),
                json.dumps(result),
                result.get("confidence_score", 0.0),
            ),
        )


def get_cycle_pnl_accuracy(cycle_id: int) -> dict:
    """Compare computed P&L (from trades DB) vs actual USDC delta (from portfolio snapshots).

    Excludes held_for_resolution trades — their P&L only becomes cash after claiming.
    Returns a dict suitable for inclusion in Claude's analysis prompt.
    """
    if not _db_path.exists():
        return {}
    with _conn() as conn:
        cycle_row = conn.execute(
            "SELECT usdc_at_start FROM learning_cycles WHERE id=?", (cycle_id,)
        ).fetchone()
        usdc_at_start = float(cycle_row[0]) if cycle_row and cycle_row[0] is not None else None

        pnl_row = conn.execute(
            """SELECT ROUND(SUM(net_pnl), 4), COUNT(*)
               FROM trades
               WHERE cycle_id=? AND status='closed' AND trigger_hit=1 AND net_pnl IS NOT NULL""",
            (cycle_id,),
        ).fetchone()
        computed_pnl = float(pnl_row[0]) if pnl_row and pnl_row[0] is not None else None
        closed_trades = int(pnl_row[1]) if pnl_row else 0

        usdc_row = conn.execute(
            "SELECT value FROM dashboard_state WHERE key='portfolio_usdc'"
        ).fetchone()
        current_usdc = float(usdc_row[0]) if usdc_row and usdc_row[0] else None

    actual_delta = None
    accuracy_ratio = None
    if usdc_at_start is not None and current_usdc is not None:
        actual_delta = round(current_usdc - usdc_at_start, 4)
        if computed_pnl and computed_pnl != 0:
            accuracy_ratio = round(actual_delta / computed_pnl, 3)

    return {
        "usdc_at_cycle_start": usdc_at_start,
        "current_usdc": current_usdc,
        "actual_usdc_delta": actual_delta,
        "computed_pnl_closed_trades": computed_pnl,
        "closed_triggered_trades": closed_trades,
        "pnl_accuracy_ratio": accuracy_ratio,
        "note": "held_for_resolution trades excluded (unclaimed = not yet in USDC balance)",
    }


def get_dominant_regime_for_cycle(cycle_id: int, phase: str = "learn") -> str | None:
    """Return the most common regime label among triggered trades in a cycle phase."""
    if not _db_path.exists():
        return None
    with _conn() as conn:
        row = conn.execute(
            """SELECT regime, COUNT(*) as n FROM trades
               WHERE cycle_id=? AND phase=? AND trigger_hit=1 AND regime IS NOT NULL
               GROUP BY regime ORDER BY n DESC LIMIT 1""",
            (cycle_id, phase),
        ).fetchone()
    return row[0] if row else None


def get_portfolio_snapshot() -> dict:
    """Read latest portfolio data from dashboard_state."""
    def _f(v: str | None) -> float | None:
        try:
            return float(v) if v else None
        except Exception:
            return None

    raw_pos = get_state("portfolio_positions")
    try:
        positions = json.loads(raw_pos) if raw_pos else []
    except Exception:
        positions = []

    return {
        "usdc": _f(get_state("portfolio_usdc")),
        "value": _f(get_state("portfolio_value")),
        "start_usdc": _f(get_state("portfolio_start_usdc")),
        "positions": positions,
        "updated_at": get_state("portfolio_updated_at"),
    }


def get_signal_lab_trades(
    coin: str | None = None,
    days: int | None = None,
    only_today: bool = False,
    triggered_only: bool = False,
    outcome: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Return paginated trades with ALL columns for Signal Lab.

    outcome: "WIN" | "LOSS" | "NO_TRIGGER" | None (all)
    Returns (rows, total_count_without_limit).
    """
    if not _db_path.exists():
        return [], 0
    conditions: list[str] = []
    params: list = []
    if coin:
        conditions.append("coin = ?")
        params.append(coin)
    if only_today:
        conditions.append("date(created_at) = date('now')")
    elif days:
        conditions.append("created_at >= datetime('now', ?)")
        params.append(f"-{days} days")
    if triggered_only:
        conditions.append("trigger_hit = 1")
    if outcome == "WIN":
        conditions.append("trigger_hit = 1 AND net_pnl > 0")
    elif outcome == "LOSS":
        conditions.append("trigger_hit = 1 AND net_pnl <= 0")
    elif outcome == "NO_TRIGGER":
        conditions.append("trigger_hit = 0 OR trigger_hit IS NULL")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with _conn() as conn:
        count_row = conn.execute(
            f"SELECT COUNT(*) FROM trades {where}", params
        ).fetchone()
        total = int(count_row[0]) if count_row else 0
        rows = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return [dict(r) for r in rows], total


def export_query_trades(
    coin: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    triggered_only: bool = False,
    outcome: str | None = None,
    limit: int = 1000,
    days: int | None = None,
    only_today: bool = False,
) -> tuple[list[dict], int]:
    """Flexible export query with explicit date range (YYYY-MM-DD strings) or
    relative days/today filters (same semantics as get_signal_lab_trades).

    Independent of the paginated get_signal_lab_trades — no offset, supports
    arbitrary date ranges, returns (rows, total_matching_count).
    """
    if not _db_path.exists():
        return [], 0
    conditions: list[str] = []
    params: list = []
    if coin:
        conditions.append("coin = ?")
        params.append(coin)
    # Explicit date range takes precedence over relative days/today
    if date_start:
        conditions.append("date(created_at) >= ?")
        params.append(date_start)
    if date_end:
        conditions.append("date(created_at) <= ?")
        params.append(date_end)
    if not date_start and not date_end:
        if only_today:
            conditions.append("date(created_at) = date('now')")
        elif days:
            conditions.append("created_at >= datetime('now', ?)")
            params.append(f"-{days} days")
    if triggered_only:
        conditions.append("trigger_hit = 1")
    if outcome == "WIN":
        conditions.append("trigger_hit = 1 AND net_pnl > 0")
    elif outcome == "LOSS":
        conditions.append("trigger_hit = 1 AND net_pnl <= 0")
    elif outcome == "NO_TRIGGER":
        conditions.append("(trigger_hit = 0 OR trigger_hit IS NULL)")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with _conn() as conn:
        count_row = conn.execute(f"SELECT COUNT(*) FROM trades {where}", params).fetchone()
        total = int(count_row[0]) if count_row else 0
        rows = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY created_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
    return [dict(r) for r in rows], total


# ── Phase 2: Signal Analytics ─────────────────────────────────────────────────

def _signal_where(
    coin: str | None,
    days: int | None,
    only_today: bool,
    extra_conditions: list[str] | None = None,
) -> tuple[str, list]:
    """Build WHERE clause + params for signal analytics queries."""
    conditions = ["trigger_hit = 1", "status IN ('closed','resolved')"]
    params: list = []
    if coin:
        conditions.append("coin = ?")
        params.append(coin)
    if only_today:
        conditions.append("date(created_at) = date('now')")
    elif days:
        conditions.append("created_at >= datetime('now', ?)")
        params.append(f"-{days} days")
    if extra_conditions:
        conditions.extend(extra_conditions)
    return " AND ".join(conditions), params


def get_conviction_bucket_stats(
    coin: str | None = None, days: int | None = None, only_today: bool = False
) -> list[dict]:
    """Win rate + avg P&L grouped by conviction_score_at_trigger bucket."""
    if not _db_path.exists():
        return []
    where, params = _signal_where(coin, days, only_today)
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT
                CASE
                  WHEN conviction_score_at_trigger IS NULL OR conviction_score_at_trigger = 0
                    THEN 'Geen signaal'
                  WHEN conviction_score_at_trigger < 0.3 THEN '0.0–0.3 zwak'
                  WHEN conviction_score_at_trigger < 0.5 THEN '0.3–0.5 matig'
                  WHEN conviction_score_at_trigger < 0.7 THEN '0.5–0.7 sterk'
                  ELSE '0.7–1.0 zeer sterk'
                END AS bucket,
                COUNT(*) AS n,
                ROUND(AVG(CASE WHEN actual_winner = winner_side THEN 1.0 ELSE 0.0 END)*100, 1) AS win_pct,
                ROUND(AVG(net_pnl), 4) AS avg_net_pnl,
                ROUND(SUM(net_pnl), 4) AS total_pnl
              FROM trades WHERE {where}
              GROUP BY bucket
              ORDER BY MIN(COALESCE(conviction_score_at_trigger, -1))""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_regime_bucket_stats(
    coin: str | None = None, days: int | None = None, only_today: bool = False
) -> list[dict]:
    """Win rate + avg P&L grouped by regime_at_entry."""
    if not _db_path.exists():
        return []
    where, params = _signal_where(coin, days, only_today)
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT
                COALESCE(regime_at_entry, 'UNKNOWN') AS regime,
                COUNT(*) AS n,
                ROUND(AVG(CASE WHEN actual_winner = winner_side THEN 1.0 ELSE 0.0 END)*100, 1) AS win_pct,
                ROUND(AVG(net_pnl), 4) AS avg_net_pnl,
                ROUND(SUM(net_pnl), 4) AS total_pnl,
                ROUND(AVG(CASE WHEN winner_exit_reason = 'peg_cross' THEN 1.0 ELSE 0.0 END)*100, 1)
                  AS peg_cross_pct
              FROM trades WHERE {where}
              GROUP BY regime
              ORDER BY total_pnl DESC""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_ofi_bucket_stats(
    coin: str | None = None, days: int | None = None, only_today: bool = False
) -> list[dict]:
    """Win rate + avg P&L grouped by OFI-at-trigger bucket."""
    if not _db_path.exists():
        return []
    where, params = _signal_where(coin, days, only_today)
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT
                CASE
                  WHEN ofi_at_trigger IS NULL THEN 'Geen data'
                  WHEN ofi_at_trigger < 0.40 THEN '<0.40 sterk bear'
                  WHEN ofi_at_trigger < 0.45 THEN '0.40–0.45 zwak bear'
                  WHEN ofi_at_trigger <= 0.55 THEN '0.45–0.55 neutraal'
                  WHEN ofi_at_trigger <= 0.60 THEN '0.55–0.60 zwak bull'
                  ELSE '>0.60 sterk bull'
                END AS bucket,
                COUNT(*) AS n,
                ROUND(AVG(CASE WHEN actual_winner = winner_side THEN 1.0 ELSE 0.0 END)*100, 1) AS win_pct,
                ROUND(AVG(net_pnl), 4) AS avg_net_pnl,
                ROUND(SUM(net_pnl), 4) AS total_pnl
              FROM trades WHERE {where}
              GROUP BY bucket
              ORDER BY MIN(COALESCE(ofi_at_trigger, -1))""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_conviction_threshold_sweep(
    coin: str | None = None, days: int | None = None, only_today: bool = False
) -> list[dict]:
    """For each threshold in [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]:
    return trades included, win%, avg_net_pnl, total_pnl.
    Allows retroactive A/B testing on existing data.
    """
    if not _db_path.exists():
        return []
    base_where, base_params = _signal_where(coin, days, only_today)
    results = []
    with _conn() as conn:
        for threshold in [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]:
            thr_where = base_where
            thr_params = list(base_params)
            if threshold > 0:
                thr_where += " AND COALESCE(conviction_score_at_trigger, 0) >= ?"
                thr_params.append(threshold)
            row = conn.execute(
                f"""SELECT COUNT(*) n,
                    ROUND(AVG(CASE WHEN actual_winner=winner_side THEN 1.0 ELSE 0.0 END)*100,1) win_pct,
                    ROUND(AVG(net_pnl),4) avg_pnl,
                    ROUND(SUM(net_pnl),4) total_pnl
                    FROM trades WHERE {thr_where}""",
                thr_params,
            ).fetchone()
            results.append({"min_conviction": threshold, **dict(row)})
    return results


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
