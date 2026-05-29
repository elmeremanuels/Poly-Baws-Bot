"""Oracle pattern mining — extracts conditional win probabilities from the trade DB.

Uses plain SQLite aggregations (no ML libraries needed).
Recency weighting is applied so recent trades count more than old ones.
"""
from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config_loader import CONFIG

_db_path = Path(__file__).parent.parent / CONFIG["logging"]["db_path"]


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path), isolation_level=None, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _recency_weight(hours_ago: float, halflife_hours: float = 6.0) -> float:
    return math.exp(-hours_ago * math.log(2) / halflife_hours)


# ── Aggregate pattern stats ────────────────────────────────────────────────────

def get_pattern_stats(coin: str | None = None, days: int = 30) -> list[dict]:
    """Conditional win chances per regime × conviction_bucket × router_bucket.

    Only includes combinations with at least 5 trades.
    """
    if not _db_path.exists():
        return []

    conditions = [
        "status IN ('closed','resolved')",
        f"created_at >= datetime('now', '-{days} days')",
    ]
    params: list[Any] = []
    if coin:
        conditions.append("coin = ?")
        params.append(coin)
    where = " AND ".join(conditions)

    sql = f"""
    SELECT
        COALESCE(regime_at_entry, 'UNKNOWN') AS regime,
        CASE
            WHEN conviction_score_at_trigger IS NULL OR conviction_score_at_trigger = 0 THEN 'geen_signaal'
            WHEN conviction_score_at_trigger < 0.55 THEN 'laag'
            WHEN conviction_score_at_trigger < 0.70 THEN 'midden'
            ELSE 'hoog'
        END AS conv_bucket,
        COALESCE(router_bucket, triggered_by, 'unknown') AS bucket,
        COUNT(*) AS n,
        ROUND(AVG(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END)*100, 1) AS win_pct,
        ROUND(AVG(net_pnl), 4) AS avg_pnl,
        ROUND(SUM(net_pnl), 4) AS total_pnl
    FROM trades
    WHERE {where}
    GROUP BY regime, conv_bucket, bucket
    HAVING n >= 5
    ORDER BY win_pct DESC
    """
    try:
        with _conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_signal_patterns(coin: str | None = None, days: int = 30) -> list[dict]:
    """OFI-bucket × funding_rate_bucket × regime → win%.

    Only includes combinations with at least 5 trades.
    """
    if not _db_path.exists():
        return []

    conditions = [
        "status IN ('closed','resolved')",
        "trigger_hit = 1",
        f"created_at >= datetime('now', '-{days} days')",
    ]
    params: list[Any] = []
    if coin:
        conditions.append("coin = ?")
        params.append(coin)
    where = " AND ".join(conditions)

    sql = f"""
    SELECT
        CASE
            WHEN ofi_at_trigger IS NULL THEN 'onbekend'
            WHEN ofi_at_trigger < 0.45 THEN '<0.45 bearish'
            WHEN ofi_at_trigger < 0.50 THEN '0.45-0.50 neutraal-'
            WHEN ofi_at_trigger < 0.55 THEN '0.50-0.55 neutraal+'
            ELSE '>0.55 bullish'
        END AS ofi_bucket,
        CASE
            WHEN funding_rate_at_trigger IS NULL THEN 'onbekend'
            WHEN funding_rate_at_trigger < -0.001 THEN 'negatief'
            WHEN funding_rate_at_trigger > 0.001 THEN 'positief'
            ELSE 'neutraal'
        END AS fr_bucket,
        COALESCE(regime_at_entry, 'UNKNOWN') AS regime,
        COUNT(*) AS n,
        ROUND(AVG(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END)*100, 1) AS win_pct,
        ROUND(AVG(net_pnl), 4) AS avg_pnl
    FROM trades
    WHERE {where}
    GROUP BY ofi_bucket, fr_bucket, regime
    HAVING n >= 5
    ORDER BY win_pct DESC
    """
    try:
        with _conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── Per-trade recency-weighted win probability ────────────────────────────────

def get_current_pattern_win_probability(
    coin: str,
    regime: str,
    conviction_score: float,
    bucket: str,
    lookback_days: int = 14,
) -> float:
    """Recency-weighted win probability for exactly this regime × conviction × bucket.

    Returns 0.5 (neutral) when fewer than 5 matching trades exist.
    """
    if not _db_path.exists():
        return 0.5

    if conviction_score < 0.55:
        conv_bucket = "laag" if conviction_score >= 0.001 else "geen_signaal"
    elif conviction_score < 0.70:
        conv_bucket = "midden"
    else:
        conv_bucket = "hoog"

    sql = """
    SELECT net_pnl, created_at
    FROM trades
    WHERE status IN ('closed', 'resolved')
      AND COALESCE(regime_at_entry, 'UNKNOWN') = ?
      AND COALESCE(router_bucket, triggered_by, 'unknown') = ?
      AND created_at >= datetime('now', ?)
      AND (
        CASE
            WHEN conviction_score_at_trigger IS NULL OR conviction_score_at_trigger = 0 THEN 'geen_signaal'
            WHEN conviction_score_at_trigger < 0.55 THEN 'laag'
            WHEN conviction_score_at_trigger < 0.70 THEN 'midden'
            ELSE 'hoog'
        END
      ) = ?
    ORDER BY created_at DESC
    LIMIT 50
    """
    try:
        with _conn() as conn:
            rows = conn.execute(
                sql, (regime, bucket, f"-{lookback_days} days", conv_bucket)
            ).fetchall()
    except Exception:
        return 0.5

    if len(rows) < 5:
        return 0.5

    now_ts = time.time()
    weighted_win = 0.0
    weight_total = 0.0

    for row in rows:
        try:
            from datetime import datetime, timezone
            pub_ts = datetime.fromisoformat(
                str(row["created_at"]).replace("Z", "+00:00")
            ).timestamp()
            hours_ago = max(0.0, (now_ts - pub_ts) / 3600)
        except Exception:
            hours_ago = 12.0
        w = _recency_weight(hours_ago)
        won = 1.0 if (row["net_pnl"] or 0) > 0 else 0.0
        weighted_win += w * won
        weight_total += w

    if weight_total <= 0:
        return 0.5
    return round(weighted_win / weight_total, 3)
