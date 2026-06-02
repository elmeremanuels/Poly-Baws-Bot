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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(event_type, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hybrid_window ON hybrid_pending(window_start)")
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


def get_router_trades(hours: int = 24, limit: int = 60) -> list[dict]:
    """Trades van de afgelopen N uur gerouteerd via auto_router (open + recent gesloten)."""
    if not _db_path.exists():
        return []
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM trades
               WHERE (router_bucket IS NOT NULL OR mode = 'auto_router')
                 AND created_at >= datetime('now', ?)
               ORDER BY created_at DESC LIMIT ?""",
            (f"-{hours} hours", limit),
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


# ── Signal accuracy per coin (uit Signal Lab / straddle data) ─────────────────

def get_signal_accuracy_per_coin(days: int | None = None,
                                  hours: int | None = None) -> list[dict]:
    """Conviction-richting accuracy per coin vanuit straddle-history (Signal Lab data).

    Vergelijkt conviction_at_trigger ('UP'/'DOWN') met actual_winner ('YES'/'NO').
    Geen dubbele tracking nodig — hergebruikt de bestaande straddle datapunten.
    Geeft per coin: n (trades met signaal), accuracy (%), net_pnl (straddle P&L).
    hours heeft prioriteit over days. Beide None → alle tijd.
    """
    if not _db_path.exists():
        return []
    conditions = [
        "trigger_hit = 1",
        "status IN ('closed','resolved')",
        "conviction_at_trigger IS NOT NULL",
        "actual_winner IS NOT NULL",
        "(mode IS NULL OR mode != 'signal_trader')",
    ]
    params: list = []
    if hours is not None:
        conditions.append("created_at >= datetime('now', ?)")
        params.append(f"-{hours} hours")
    elif days:
        conditions.append("created_at >= datetime('now', ?)")
        params.append(f"-{days} days")
    where = " AND ".join(conditions)
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT
                coin,
                COUNT(*) AS n,
                ROUND(
                    SUM(CASE
                        WHEN (conviction_at_trigger = 'UP'   AND actual_winner = 'YES')
                          OR (conviction_at_trigger = 'DOWN' AND actual_winner = 'NO')
                        THEN 1.0 ELSE 0.0
                    END) / NULLIF(COUNT(*), 0) * 100
                , 1) AS accuracy_pct,
                ROUND(SUM(net_pnl), 4) AS total_pnl
            FROM trades
            WHERE {where}
            GROUP BY coin
            ORDER BY coin""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_market_resolution(market_id: str) -> str | None:
    """Zoek de daadwerkelijke winnaar op voor een market via straddle-trades.

    Geeft 'YES' of 'NO' terug als er een afgesloten straddle-trade bestaat voor
    deze market_id. Wordt gebruikt als backup in signal_trader resolution wanneer
    de WS-prijs niet beschikbaar is na settlement.
    """
    if not _db_path.exists() or not market_id:
        return None
    with _conn() as conn:
        row = conn.execute(
            """SELECT actual_winner FROM trades
               WHERE market_id = ?
                 AND actual_winner IS NOT NULL
                 AND (mode IS NULL OR mode != 'signal_trader')
               LIMIT 1""",
            (market_id,),
        ).fetchone()
    return row["actual_winner"] if row else None


# ── Signal Trader ─────────────────────────────────────────────────────────────

def get_signal_trades(days: int | None = None,
                      hours: int | None = None) -> list[dict]:
    """Alle signal_trader trades, meest recent eerst.

    hours heeft prioriteit over days. Beide None → alles.
    """
    if not _db_path.exists():
        return []
    with _conn() as conn:
        if hours is not None:
            rows = conn.execute(
                "SELECT * FROM trades WHERE mode='signal_trader'"
                " AND created_at >= datetime('now', ?)"
                " ORDER BY created_at DESC",
                (f"-{hours} hours",),
            ).fetchall()
        elif days is not None:
            rows = conn.execute(
                "SELECT * FROM trades WHERE mode='signal_trader'"
                " AND created_at >= datetime('now', ?)"
                " ORDER BY created_at DESC",
                (f"-{days} days",),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades WHERE mode='signal_trader'"
                " ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def get_signal_trades_today() -> list[dict]:
    """Signal_trader trades van vandaag (UTC kalenderdag)."""
    if not _db_path.exists():
        return []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE mode='signal_trader'"
            " AND date(created_at)=?"
            " ORDER BY created_at DESC",
            (today,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_signal_trade_stats(days: int | None = None) -> dict:
    """
    Geaggregeerde stats voor signal_trader trades.
    Retourneert: {total, won, lost, pending, accuracy, net_pnl, by_coin}
    """
    if not _db_path.exists():
        return {}
    with _conn() as conn:
        if days is not None:
            where = "mode='signal_trader' AND created_at >= datetime('now', ?)"
            params: tuple = (f"-{days} days",)
        else:
            where = "mode='signal_trader'"
            params = ()

        row = conn.execute(
            f"""SELECT
                COUNT(*) total,
                SUM(CASE WHEN winner_exit_reason='resolution_won' THEN 1 ELSE 0 END) won,
                SUM(CASE WHEN winner_exit_reason='resolution_lost' THEN 1 ELSE 0 END) lost,
                SUM(CASE WHEN status='signal_holding' THEN 1 ELSE 0 END) pending,
                ROUND(COALESCE(SUM(net_pnl), 0), 4) net_pnl
            FROM trades WHERE {where}""",
            params,
        ).fetchone()

        if not row:
            return {}

        total  = int(row["total"])
        won    = int(row["won"])
        lost   = int(row["lost"])
        pending = int(row["pending"])
        net_pnl = float(row["net_pnl"])
        closed  = won + lost
        accuracy = won / closed * 100.0 if closed else 0.0

        # Per-coin breakdown
        coin_rows = conn.execute(
            f"""SELECT coin,
                COUNT(*) n,
                SUM(CASE WHEN winner_exit_reason='resolution_won' THEN 1 ELSE 0 END) won,
                SUM(CASE WHEN winner_exit_reason='resolution_lost' THEN 1 ELSE 0 END) lost,
                ROUND(COALESCE(SUM(net_pnl), 0), 4) net_pnl
            FROM trades WHERE {where}
            GROUP BY coin ORDER BY coin""",
            params,
        ).fetchall()

    return {
        "total": total,
        "won": won,
        "lost": lost,
        "pending": pending,
        "closed": closed,
        "accuracy": round(accuracy, 1),
        "net_pnl": net_pnl,
        "by_coin": [dict(r) for r in coin_rows],
    }


def get_signal_trades_paginated(
    hours: int | None = None,
    page: int = 0,
    per_page: int = 20,
    exclude_status: list[str] | None = None,
) -> tuple[list[dict], int]:
    """Pagineerd ophalen van signal_trader trades (afgeronde trades).

    Geeft (rows, total_count) terug.
    exclude_status: lijst van statussen die uitgesloten worden (bv. ['signal_holding'])
    """
    if not _db_path.exists():
        return [], 0
    exclude_status = exclude_status or ["signal_holding"]
    placeholders = ",".join("?" for _ in exclude_status)
    if hours is not None:
        where = (f"mode='signal_trader' AND status NOT IN ({placeholders})"
                 f" AND created_at >= datetime('now', ?)")
        params_count = [*exclude_status, f"-{hours} hours"]
    else:
        where = f"mode='signal_trader' AND status NOT IN ({placeholders})"
        params_count = list(exclude_status)

    with _conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM trades WHERE {where}", params_count
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM trades WHERE {where}"
            f" ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params_count, per_page, page * per_page],
        ).fetchall()
    return [dict(r) for r in rows], int(total)


def get_signal_active_trades() -> list[dict]:
    """Signal_trader trades die momenteel open zijn (status=signal_holding)."""
    if not _db_path.exists():
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE mode='signal_trader' AND status='signal_holding'"
            " ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_signal_trades(status: str | None = None) -> int:
    """Verwijder signal_trader trades uit de DB.
    status=None → alle signal_trader trades; anders alleen die status.
    Geeft het aantal verwijderde rijen terug.
    """
    if not _db_path.exists():
        return 0
    with _conn() as conn:
        if status:
            cur = conn.execute(
                "DELETE FROM trades WHERE mode='signal_trader' AND status=?",
                (status,),
            )
        else:
            cur = conn.execute("DELETE FROM trades WHERE mode='signal_trader'")
        return cur.rowcount


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
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
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

def _date_where(conditions: list, params: list, days: int | None, only_today: bool,
                date_from: str | None = None, date_to: str | None = None) -> None:
    """Centraliseert datumfilter-logica. Aangepast bereik overschrijft days/today."""
    if date_from and date_to:
        conditions.append("date(created_at) BETWEEN ? AND ?")
        params.extend([date_from, date_to])
    elif date_from:
        conditions.append("date(created_at) >= ?")
        params.append(date_from)
    elif only_today:
        conditions.append("date(created_at) = date('now')")
    elif days:
        conditions.append("created_at >= datetime('now', ?)")
        params.append(f"-{days} days")


# Mode filter SQL snippets — None = geen filter (alle modes)
_MODE_SQL: dict[str | None, str | None] = {
    None:          None,
    "straddle":    "(mode IS NULL OR mode NOT IN ('signal_trader', 'auto_router'))",
    "signal":      "mode = 'signal_trader'",
    "auto_router": "mode = 'auto_router'",
}


def get_analytics_trades(coin: str | None = None, days: int | None = None,
                         only_today: bool = False,
                         mode_filter: str | None = "straddle",
                         date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    """Trades gefilterd op mode, gesorteerd op created_at ASC.
    mode_filter=None toont alle modes; standaard 'straddle' (achterwaarts compatibel).
    """
    if not _db_path.exists():
        return []
    conditions: list[str] = []
    mode_sql = _MODE_SQL.get(mode_filter)
    if mode_sql:
        conditions.append(mode_sql)
    params: list = []
    if coin:
        conditions.append("coin = ?")
        params.append(coin)
    _date_where(conditions, params, days, only_today, date_from, date_to)
    where = "WHERE " + " AND ".join(conditions)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY created_at ASC",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_exit_reason_stats(coin: str | None = None, days: int | None = None,
                          only_today: bool = False,
                          mode_filter: str | None = "straddle",
                          date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    """Aggregate stats grouped by winner_exit_reason."""
    if not _db_path.exists():
        return []
    conditions = ["status IN ('closed','resolved')", "trigger_hit = 1"]
    mode_sql = _MODE_SQL.get(mode_filter)
    if mode_sql:
        conditions.append(mode_sql)
    params: list = []
    if coin:
        conditions.append("coin = ?")
        params.append(coin)
    _date_where(conditions, params, days, only_today, date_from, date_to)
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


def get_coin_comparison(days: int | None = None, only_today: bool = False,
                        mode_filter: str | None = "straddle",
                        date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    """Per-coin aggregated stats for closed triggered trades."""
    if not _db_path.exists():
        return []
    conditions = ["status IN ('closed','resolved')", "trigger_hit = 1"]
    mode_sql = _MODE_SQL.get(mode_filter)
    if mode_sql:
        conditions.append(mode_sql)
    params: list = []
    _date_where(conditions, params, days, only_today, date_from, date_to)
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
                   only_today: bool = False,
                   mode_filter: str | None = "straddle",
                   date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    """Average P&L by hour of day (UTC)."""
    if not _db_path.exists():
        return []
    conditions = ["status IN ('closed','resolved')", "trigger_hit = 1"]
    mode_sql = _MODE_SQL.get(mode_filter)
    if mode_sql:
        conditions.append(mode_sql)
    params: list = []
    if coin:
        conditions.append("coin = ?")
        params.append(coin)
    _date_where(conditions, params, days, only_today, date_from, date_to)
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


def get_mode_comparison(days: int | None = None, only_today: bool = False) -> list[dict]:
    """Per-mode aggregated stats — altijd alle modes, geen mode_filter."""
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
                COALESCE(mode, 'straddle') AS trade_mode,
                COUNT(*) AS n,
                ROUND(AVG(CASE WHEN actual_winner = winner_side THEN 1.0 ELSE 0.0 END)*100, 1) AS win_pct,
                ROUND(AVG(net_pnl), 4) AS avg_pnl,
                ROUND(SUM(net_pnl), 4) AS total_pnl
            FROM trades
            WHERE {where}
            GROUP BY COALESCE(mode, 'straddle')
            ORDER BY total_pnl DESC""",
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


def get_learn_coin_counts(cycle_id: int) -> dict[str, int]:
    """Per-coin trade count in the learn phase of a cycle (drives coverage progress)."""
    if not _db_path.exists():
        return {}
    with _conn() as conn:
        rows = conn.execute(
            "SELECT coin, COUNT(*) AS n FROM trades WHERE cycle_id=? AND phase='learn' GROUP BY coin",
            (cycle_id,),
        ).fetchall()
    return {r["coin"]: r["n"] for r in rows}


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

    # Enrich each coin with regime + conviction distribution within this cycle
    for coin in per_coin:
        try:
            reg_rows = conn.execute(
                """SELECT COALESCE(regime_at_entry,'UNKNOWN') AS regime,
                          COUNT(*) n,
                          ROUND(AVG(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END)*100,1) win_pct,
                          ROUND(AVG(net_pnl),4) avg_pnl
                   FROM trades WHERE cycle_id=? AND coin=? AND trigger_hit=1
                   GROUP BY regime ORDER BY n DESC""",
                (cycle_id, coin),
            ).fetchall()
            per_coin[coin]["regime_distribution"] = {
                r["regime"]: {"n": r["n"], "win_pct": r["win_pct"], "avg_pnl": r["avg_pnl"]}
                for r in reg_rows
            }
            conv_rows = conn.execute(
                """SELECT
                     CASE
                       WHEN conviction_score_at_trigger IS NULL OR conviction_score_at_trigger=0 THEN 'none'
                       WHEN conviction_score_at_trigger < 0.3 THEN 'low'
                       WHEN conviction_score_at_trigger < 0.6 THEN 'medium'
                       ELSE 'high'
                     END AS bucket,
                     COUNT(*) n,
                     ROUND(AVG(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END)*100,1) win_pct,
                     ROUND(AVG(net_pnl),4) avg_pnl
                   FROM trades WHERE cycle_id=? AND coin=? AND trigger_hit=1
                   GROUP BY bucket""",
                (cycle_id, coin),
            ).fetchall()
            per_coin[coin]["conviction_distribution"] = {
                r["bucket"]: {"n": r["n"], "win_pct": r["win_pct"], "avg_pnl": r["avg_pnl"]}
                for r in conv_rows
            }
            early_row = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE cycle_id=? AND coin=? AND early_loser_side IS NOT NULL",
                (cycle_id, coin),
            ).fetchone()
            per_coin[coin]["early_loser_count"] = early_row[0] if early_row else 0
        except Exception:
            pass  # never break existing cycle stats on enrichment errors

    return {
        "per_coin": per_coin,
        "total_trades": total[0] if total else 0,
        "overall_avg_pnl": total[1] if total else None,
    }


def get_pattern_stats(
    coin: str | None = None,
    days: int | None = 90,
) -> list[dict]:
    """Historical straddle outcomes grouped by (regime, conviction_bucket, ofi_bucket).

    Used by pattern_matcher to find how trades fared under similar conditions.
    Excludes signal_trader mode — patterns (trailing, peg-cross) don't apply there.
    """
    if not _db_path.exists():
        return []
    conditions = ["trigger_hit = 1", "status IN ('closed','resolved')",
                  "(mode IS NULL OR mode != 'signal_trader')"]
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
                  COALESCE(regime_at_entry, 'NORMAL') AS regime,
                  CASE
                    WHEN conviction_score_at_trigger IS NULL OR conviction_score_at_trigger = 0 THEN 'none'
                    WHEN conviction_score_at_trigger < 0.3 THEN 'low'
                    WHEN conviction_score_at_trigger < 0.6 THEN 'medium'
                    ELSE 'high'
                  END AS conviction_bucket,
                  CASE
                    WHEN ofi_at_trigger IS NULL THEN 'unknown'
                    WHEN ofi_at_trigger <= 0.40 THEN 'bear'
                    WHEN ofi_at_trigger >= 0.60 THEN 'bull'
                    ELSE 'neutral'
                  END AS ofi_bucket,
                  COUNT(*) AS n,
                  ROUND(AVG(CASE WHEN actual_winner = winner_side THEN 1.0 ELSE 0.0 END)*100, 1) AS win_pct,
                  ROUND(AVG(net_pnl), 4) AS avg_pnl,
                  ROUND(SUM(net_pnl), 4) AS total_pnl,
                  ROUND(AVG(loser_exit_price), 3) AS avg_loser_price
                FROM trades WHERE {where}
                GROUP BY regime, conviction_bucket, ofi_bucket
                ORDER BY n DESC""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


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
    mode_filter: str | None = "straddle",
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[str, list]:
    """Build WHERE clause + params for signal analytics queries."""
    conditions = ["trigger_hit = 1", "status IN ('closed','resolved')"]
    mode_sql = _MODE_SQL.get(mode_filter)
    if mode_sql:
        conditions.append(mode_sql)
    params: list = []
    if coin:
        conditions.append("coin = ?")
        params.append(coin)
    _date_where(conditions, params, days, only_today, date_from, date_to)
    if extra_conditions:
        conditions.extend(extra_conditions)
    return " AND ".join(conditions), params


def get_conviction_bucket_stats(
    coin: str | None = None, days: int | None = None, only_today: bool = False,
    mode_filter: str | None = "straddle",
    date_from: str | None = None, date_to: str | None = None,
) -> list[dict]:
    """Win rate + avg P&L grouped by conviction_score_at_trigger bucket."""
    if not _db_path.exists():
        return []
    where, params = _signal_where(coin, days, only_today, mode_filter=mode_filter,
                                  date_from=date_from, date_to=date_to)
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
    coin: str | None = None, days: int | None = None, only_today: bool = False,
    mode_filter: str | None = "straddle",
    date_from: str | None = None, date_to: str | None = None,
) -> list[dict]:
    """Win rate + avg P&L grouped by regime_at_entry."""
    if not _db_path.exists():
        return []
    where, params = _signal_where(coin, days, only_today, mode_filter=mode_filter,
                                  date_from=date_from, date_to=date_to)
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
    coin: str | None = None, days: int | None = None, only_today: bool = False,
    mode_filter: str | None = "straddle",
    date_from: str | None = None, date_to: str | None = None,
) -> list[dict]:
    """Win rate + avg P&L grouped by OFI-at-trigger bucket."""
    if not _db_path.exists():
        return []
    where, params = _signal_where(coin, days, only_today, mode_filter=mode_filter,
                                  date_from=date_from, date_to=date_to)
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
    coin: str | None = None, days: int | None = None, only_today: bool = False,
    mode_filter: str | None = "straddle",
    date_from: str | None = None, date_to: str | None = None,
) -> list[dict]:
    """For each threshold in [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]:
    return trades included, win%, avg_net_pnl, total_pnl.
    Allows retroactive A/B testing on existing data.
    """
    if not _db_path.exists():
        return []
    base_where, base_params = _signal_where(coin, days, only_today, mode_filter=mode_filter,
                                            date_from=date_from, date_to=date_to)
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


def get_directional_stats(
    coin: str | None = None, days: int | None = None, only_today: bool = False
) -> list[dict]:
    """Win rate + P&L grouped by entry_type (directional vs straddle)."""
    if not _db_path.exists():
        return []
    where, params = _signal_where(coin, days, only_today)
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT
                COALESCE(entry_type, 'straddle') AS entry_type,
                COUNT(*) AS n,
                ROUND(AVG(CASE WHEN actual_winner = winner_side THEN 1.0 ELSE 0.0 END)*100, 1) AS win_pct,
                ROUND(AVG(net_pnl), 4) AS avg_net_pnl,
                ROUND(SUM(net_pnl), 4) AS total_pnl,
                ROUND(AVG(CASE WHEN winner_exit_reason='directional_wrong_side' THEN 1.0 ELSE 0.0 END)*100, 1)
                  AS wrong_side_pct
              FROM trades WHERE {where}
              GROUP BY entry_type
              ORDER BY total_pnl DESC""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_pnl_by_exit_reason(
    coin: str | None = None, days: int | None = None, only_today: bool = False
) -> list[dict]:
    """P&L breakdown per winner_exit_reason — quantifies which exit channel causes the most losses.

    Returns rows ordered by total_pnl ascending (worst at top) so the biggest loss driver
    is immediately visible in the dashboard.
    """
    if not _db_path.exists():
        return []
    conditions, params = ["trigger_hit = 1", "status IN ('closed','resolved')"], []
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
                COALESCE(winner_exit_reason, 'unknown') AS exit_reason,
                COUNT(*) AS n,
                ROUND(SUM(net_pnl), 4) AS total_pnl,
                ROUND(AVG(net_pnl), 4) AS avg_pnl,
                ROUND(AVG(CASE WHEN net_pnl < 0 THEN net_pnl ELSE NULL END), 4) AS avg_loss,
                ROUND(SUM(CASE WHEN net_pnl < 0 THEN net_pnl ELSE 0 END), 4) AS total_loss,
                ROUND(AVG(winner_exit_price), 4) AS avg_exit_price
              FROM trades WHERE {where}
              GROUP BY exit_reason
              ORDER BY total_pnl ASC""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


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


# ── Oracle pattern stats ───────────────────────────────────────────────────────

def get_oracle_pattern_stats(coin: str | None = None, days: int = 30) -> list[dict]:
    """Win% per regime × conviction_bucket × bucket. Delegates to oracle_patterns."""
    try:
        from .oracle_patterns import get_pattern_stats
        return get_pattern_stats(coin=coin, days=days)
    except Exception:
        return []


def get_oracle_signal_patterns(coin: str | None = None, days: int = 30) -> list[dict]:
    """Win% per OFI_bucket × funding_rate_bucket × regime. Delegates to oracle_patterns."""
    try:
        from .oracle_patterns import get_signal_patterns
        return get_signal_patterns(coin=coin, days=days)
    except Exception:
        return []


def get_oracle_verdicts(limit: int = 50) -> list[dict]:
    """Recent oracle verdicts from DB."""
    if not _db_path.exists():
        return []
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT * FROM oracle_verdicts ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []



# ── Whale Tracker reads ────────────────────────────────────────────────────────

def get_whale_meta() -> list[dict]:
    """Tracked addresses with last sync time and counts."""
    if not _db_path.exists():
        return []
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT address, name, last_synced_at, activity_count, positions_count, history_loaded "
                "FROM whale_meta ORDER BY name"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_whale_activity(
    address: str | None = None,
    coin: str | None = None,
    limit: int = 200,
    days: int | None = None,
) -> list[dict]:
    """Recent whale activity, optionally filtered by address/coin/days."""
    if not _db_path.exists():
        return []
    conditions: list[str] = []
    params: list = []
    if address:
        conditions.append("address = ?")
        params.append(address)
    if coin:
        conditions.append("coin = ?")
        params.append(coin)
    if days:
        conditions.append("event_ts >= datetime('now', ?)")
        params.append(f"-{days} days")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    try:
        with _conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM whale_activity {where} ORDER BY event_ts DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_whale_positions(
    address: str | None = None,
    coin: str | None = None,
    active_only: bool = False,
) -> list[dict]:
    """Whale open positions, optionally filtered."""
    if not _db_path.exists():
        return []
    conditions: list[str] = []
    params: list = []
    if address:
        conditions.append("address = ?")
        params.append(address)
    if coin:
        conditions.append("coin = ?")
        params.append(coin)
    if active_only:
        conditions.append("is_redeemable = 0 AND cur_price > 0.01")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    try:
        with _conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM whale_positions {where} ORDER BY ABS(cash_pnl) DESC",
                params,
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_whale_bot_overlap(days: int = 7) -> list[dict]:
    """Markets where whale traded AND our bot had a trade in same window."""
    if not _db_path.exists():
        return []
    try:
        with _conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    t.coin,
                    t.question AS bot_question,
                    t.winner_side,
                    t.net_pnl,
                    t.created_at AS bot_ts,
                    wa.name AS whale_name,
                    wa.outcome_side AS whale_side,
                    wa.price AS whale_price,
                    wa.usdc_size AS whale_usdc,
                    wa.trade_type,
                    wa.event_ts AS whale_ts
                FROM trades t
                JOIN whale_activity wa
                  ON t.coin = wa.coin
                  AND ABS(julianday(t.created_at) - julianday(wa.event_ts)) < 0.02  -- within ~30 min
                WHERE t.status IN ('closed','resolved')
                  AND t.created_at >= datetime('now', ?)
                ORDER BY t.created_at DESC
                LIMIT 100
                """,
                (f"-{days} days",),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── BGGDSB reads ──────────────────────────────────────────────────────────────

_IS5_ADDRESS = "0x2bc01f3ad80e31f5bf3d80775b044f0c67797871"


def set_dashboard_state(key: str, value: str) -> None:
    """Sync write to dashboard_state table (for use from Streamlit threads)."""
    if not _db_path.exists():
        return
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(str(_db_path), timeout=10)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO dashboard_state (key, value, updated_at) "
            "VALUES (?, ?, datetime('now'))",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def is5_live_status() -> dict:
    """Check if is5minfixedyet has new activity in the last 15 minutes.

    Uses synced_at (our insert time) rather than event_ts to avoid timezone
    parsing. Since we sync every 5 min, new rows with synced_at < 10 min ago
    means they just traded.
    """
    if not _db_path.exists():
        return {"live": False, "last_seen": "DB niet gevonden", "trades_recent": 0}
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*), MAX(synced_at) FROM whale_activity "
                "WHERE address = ? AND synced_at >= datetime('now', '-15 minutes')",
                (_IS5_ADDRESS,),
            ).fetchone()
            n_recent = row[0] if row else 0
            last_synced = (row[1] or "")[:16] if row else ""

            # Get last seen (most recent event_ts regardless)
            last_row = conn.execute(
                "SELECT MAX(synced_at) FROM whale_activity WHERE address = ?",
                (_IS5_ADDRESS,),
            ).fetchone()
            last_seen = (last_row[0] or "onbekend")[:16] if last_row else "onbekend"

        return {
            "live": n_recent > 0,
            "trades_recent": n_recent,
            "last_seen": last_seen,
        }
    except Exception:
        return {"live": False, "last_seen": "fout", "trades_recent": 0}


def is5_recently_active(coin: str | None = None, minutes: int = 15) -> bool:
    """True if is5 has new activity synced in last N minutes (optionally filtered by coin)."""
    if not _db_path.exists():
        return False
    try:
        conditions = ["address = ?", "synced_at >= datetime('now', ?)"]
        params: list = [_IS5_ADDRESS, f"-{minutes} minutes"]
        if coin:
            conditions.append("coin = ?")
            params.append(coin)
        with _conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM whale_activity WHERE {' AND '.join(conditions)}",
                params,
            ).fetchone()
        return row[0] > 0
    except Exception:
        return False


def get_is5_recent_side(coin: str, minutes: int = 15) -> str | None:
    """Return is5's most recent outcome_side ('YES'/'NO') for coin within last N minutes.

    Used as a tiebreaker when yes_ask ≈ no_ask at window entry.
    Returns None if no recent activity or data unavailable.
    """
    if not _db_path.exists():
        return None
    try:
        with _conn() as conn:
            row = conn.execute(
                """
                SELECT outcome_side FROM whale_activity
                WHERE address = ?
                  AND coin = ?
                  AND synced_at >= datetime('now', ?)
                  AND outcome_side IN ('YES', 'NO')
                ORDER BY event_ts DESC LIMIT 1
                """,
                (_IS5_ADDRESS, coin, f"-{minutes} minutes"),
            ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def recalculate_bggdsb_pnl() -> int:
    """Retroactieve P&L-correctie voor alle gesloten BGGDSB-trades.

    Correcte formule: gross_pnl = winner_shares × 1.00 - total_invested
                      net_pnl   = gross_pnl - fees_paid

    Voor trades waarbij gross_pnl al correct was opgeslagen (hold-task liep
    netjes af): alleen net_pnl bijwerken (= gross_pnl - fees_paid).
    Voor trades waarbij gross_pnl NULL is (bot crash mid-window): herbereken
    uit de DB-kolommen (initiële entry only — extra buys niet bekend).

    Geeft het aantal bijgewerkte rijen terug.
    """
    if not _db_path.exists():
        return 0
    with _conn() as conn:
        # Stap 1: vul gross_pnl in voor trades waar het nog NULL is
        # formule: winner_shares × 1.00 − (entry_yes_price × yes_size + entry_no_price × no_size)
        conn.execute("""
            UPDATE trades
            SET gross_pnl = ROUND(
                CASE winner_side
                  WHEN 'YES' THEN COALESCE(yes_size, 0) * 1.0
                               - (COALESCE(entry_yes_price, 0) * COALESCE(yes_size, 0)
                                + COALESCE(entry_no_price,  0) * COALESCE(no_size,  0))
                  WHEN 'NO'  THEN COALESCE(no_size,  0) * 1.0
                               - (COALESCE(entry_yes_price, 0) * COALESCE(yes_size, 0)
                                + COALESCE(entry_no_price,  0) * COALESCE(no_size,  0))
                  ELSE NULL
                END, 4)
            WHERE router_bucket = 'bggdsb'
              AND COALESCE(triggered_by, '') NOT IN ('bggdsb_shadow')
              AND status IN ('closed', 'resolved')
              AND gross_pnl IS NULL
              AND winner_side IS NOT NULL
        """)
        # Stap 2: net_pnl = gross_pnl − fees_paid voor alle bggdsb trades.
        # Geen `AND gross_pnl IS NOT NULL` guard: COALESCE(gross_pnl, 0) behandelt
        # NULL-gross als 0 zodat elke gesloten trade een net_pnl krijgt.
        cur = conn.execute("""
            UPDATE trades
            SET net_pnl = ROUND(COALESCE(gross_pnl, 0) - COALESCE(fees_paid, 0), 4)
            WHERE router_bucket = 'bggdsb'
              AND COALESCE(triggered_by, '') NOT IN ('bggdsb_shadow')
              AND status IN ('closed', 'resolved')
              AND winner_side IS NOT NULL
        """)
        updated = cur.rowcount
    return updated


def _bggdsb_mode_clause(mode_filter: str | None) -> tuple[str, list]:
    """SQL clause + params for a BGGDSB mode filter.

    Includes legacy mode values: pre-fix paper trades were stored as 'paper'
    (not 'bggdsb_paper') and live as 'live', so match both for backward compat.
    """
    if not mode_filter:
        return "", []
    if mode_filter == "bggdsb_paper":
        return " AND mode IN ('bggdsb_paper', 'paper')", []
    if mode_filter == "bggdsb_live":
        return " AND mode IN ('bggdsb_live', 'live')", []
    return " AND mode = ?", [mode_filter]


def get_bggdsb_stats(mode_filter: str | None = None) -> dict:
    """Performance stats for BGGDSB trades.

    Matches on router_bucket='bggdsb' (new trades) OR mode IN (bggdsb modes,
    legacy 'paper'/'live') so old trades stored before mode renaming are included.
    mode_filter: 'bggdsb_live', 'bggdsb_paper', or None (alle modes).
    """
    if not _db_path.exists():
        return {}
    try:
        with _conn() as conn:
            # Schaduw-trades (achtergrond paper op niet-actieve munten) tellen NIET
            # mee in de zichtbare stats — die zijn alleen voor het geschiktheidsbord.
            # OR-condition: router_bucket covers new trades; mode covers legacy trades
            # that were stored as mode='paper'/'live' before the bggdsb_ prefix was added.
            where = ("(router_bucket = 'bggdsb' OR mode IN ('bggdsb_paper','bggdsb_live','paper','live'))"
                     " AND status IN ('closed', 'resolved') "
                     "AND COALESCE(triggered_by, '') != 'bggdsb_shadow'")
            _mode_sql, params = _bggdsb_mode_clause(mode_filter)
            where += _mode_sql
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS n,
                    ROUND(AVG(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END)*100, 1) AS win_pct,
                    ROUND(SUM(net_pnl), 4) AS total_pnl,
                    ROUND(AVG(net_pnl), 4) AS avg_pnl,
                    ROUND(AVG(entry_yes_price), 3) AS gem_entry_prijs
                FROM trades
                WHERE {where}
                """,
                params,
            ).fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}


def get_bggdsb_trades(limit: int = 100, mode_filter: str | None = None) -> list[dict]:
    """Recent BGGDSB trades ordered by creation time.

    Matches on router_bucket='bggdsb' OR legacy mode values so old trades
    stored as mode='paper' before the rename are included.
    mode_filter: 'bggdsb_live', 'bggdsb_paper', or None (alle modes).
    """
    if not _db_path.exists():
        return []
    try:
        with _conn() as conn:
            where = ("(router_bucket = 'bggdsb' OR mode IN ('bggdsb_paper','bggdsb_live','paper','live'))"
                     " AND COALESCE(triggered_by, '') != 'bggdsb_shadow'")
            _mode_sql, params = _bggdsb_mode_clause(mode_filter)
            where += _mode_sql
            params.append(limit)
            rows = conn.execute(
                f"""
                SELECT trade_id, created_at, coin, mode, winner_side,
                       entry_yes_price, entry_no_price, yes_size, no_size,
                       winner_exit_reason, net_pnl, status
                FROM trades
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_bggdsb_coin_scoreboard(coins: list[str], lookback: int = 15) -> dict[str, dict]:
    """Geschiktheid per munt op basis van recente BGGDSB-uitkomsten.

    Combineert actieve (triggered_by='bggdsb') én schaduw paper-trades
    (triggered_by='bggdsb_shadow') zodat ELKE munt een score krijgt — ook de
    munten waar we live niet in zitten.

    Recency-weging: de laatste 2–3 trades wegen het zwaarst (exponentieel
    verval met halfwaarde ~3 trades). suitability_pct = gewogen win%.

    Returns {coin: {n, suitability_pct (0-100 of None), recent_pnl}}.
    """
    out: dict[str, dict] = {c: {"n": 0, "suitability_pct": None, "recent_pnl": None}
                            for c in coins}
    if not _db_path.exists():
        return out
    try:
        with _conn() as conn:
            for coin in coins:
                rows = conn.execute(
                    """
                    SELECT net_pnl FROM trades
                    WHERE router_bucket = 'bggdsb'
                      AND COALESCE(triggered_by, '') IN ('bggdsb', 'bggdsb_shadow')
                      AND status IN ('closed', 'resolved')
                      AND winner_side IS NOT NULL
                      AND coin = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (coin, lookback),
                ).fetchall()
                if not rows:
                    continue
                num = den = 0.0
                recent_pnl = 0.0
                for i, r in enumerate(rows):
                    if r["net_pnl"] is None:
                        # Incomplete trade (logging failed) — skip rather than
                        # counting as a loss; would wrongly zero out suitability.
                        continue
                    pnl = float(r["net_pnl"])
                    w = 0.5 ** (i / 3.0)          # newest=1.0, i=3→0.5, i=6→0.25
                    num += w * (1.0 if pnl > 0 else 0.0)
                    den += w
                    if i < 3:
                        recent_pnl += pnl
                out[coin] = {
                    "n": len(rows),
                    "suitability_pct": round(100.0 * num / den, 1) if den else None,
                    "recent_pnl": round(recent_pnl, 2),
                }
        return out
    except Exception:
        return out
