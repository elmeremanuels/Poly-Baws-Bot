"""Stoplicht Scalper dashboard tab."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from src.config_loader import CONFIG


def _get_db_path():
    from pathlib import Path
    return Path(__file__).parent / CONFIG["logging"]["db_path"]


@st.cache_data(ttl=5)
def _q_recent_windows(coin: str, limit: int = 30) -> list[dict]:
    import sqlite3
    try:
        con = sqlite3.connect(str(_get_db_path()))
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT window_id, coin, window_start, stoplicht, direction,
                   entry_price, exit_price, exit_reason, pnl_eur,
                   hold_threshold_used, paper, created_at
            FROM window_tradelog
            WHERE coin = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (coin, limit)).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


@st.cache_data(ttl=3)
def _q_stoplicht_state(coin: str) -> dict:
    """Read last saved stoplicht state from dashboard_state table."""
    import sqlite3
    try:
        con = sqlite3.connect(str(_get_db_path()))
        row = con.execute(
            "SELECT value FROM dashboard_state WHERE key = ?",
            (f"scalper_stoplicht_{coin}",)
        ).fetchone()
        con.close()
        if row and row[0]:
            return json.loads(row[0])
    except Exception:
        pass
    return {}


@st.cache_data(ttl=5)
def _q_stats(coin: str) -> dict:
    import sqlite3
    try:
        con = sqlite3.connect(str(_get_db_path()))
        row = con.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN exit_reason NOT IN ('no_entry_signal','no_price_data','stoplicht_changed') THEN 1 ELSE 0 END) AS traded,
                   SUM(CASE WHEN pnl_eur > 0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(SUM(pnl_eur), 4) AS total_pnl,
                   ROUND(AVG(CASE WHEN exit_reason NOT IN ('no_entry_signal','no_price_data','stoplicht_changed') THEN pnl_eur END), 4) AS avg_pnl
            FROM window_tradelog WHERE coin = ?
        """, (coin,)).fetchone()
        con.close()
        if row:
            return dict(zip(["total", "traded", "wins", "total_pnl", "avg_pnl"], row))
    except Exception:
        pass
    return {"total": 0, "traded": 0, "wins": 0, "total_pnl": 0.0, "avg_pnl": 0.0}


@st.cache_data(ttl=5)
def _q_stoplicht_distribution(coin: str) -> dict:
    import sqlite3
    try:
        con = sqlite3.connect(str(_get_db_path()))
        rows = con.execute("""
            SELECT stoplicht, COUNT(*) AS n
            FROM window_tradelog WHERE coin = ?
            GROUP BY stoplicht
        """, (coin,)).fetchall()
        con.close()
        return {r[0]: r[1] for r in rows if r[0]}
    except Exception:
        return {}


def _color_exit(val: str) -> str:
    colors = {
        "held_to_end": "background-color:#1a3a1a;color:#4ade80",
        "trail_stop": "background-color:#2a2a1a;color:#facc15",
        "mom_reversal": "background-color:#3a1a1a;color:#f87171",
        "force_exit": "background-color:#1a1a2a;color:#94a3b8",
        "no_entry_signal": "background-color:#111;color:#555",
    }
    return colors.get(val, "")


def _color_pnl(val) -> str:
    try:
        v = float(val)
        if v > 0:
            return "color:#4ade80"
        if v < 0:
            return "color:#f87171"
    except Exception:
        pass
    return ""


def scalper_panel() -> None:
    cfg = CONFIG.get("stoplicht_scalper", {})
    coin = cfg.get("coin", "BTC")
    paper = cfg.get("paper_mode", True)
    enabled = cfg.get("enabled", False)

    # Header
    st.subheader(f"Stoplicht Scalper — {coin} 15m {'(paper)' if paper else '(live)'}")

    if not enabled:
        st.info("Scalper is uitgeschakeld (`stoplicht_scalper.enabled: false` in config.yaml). "
                "Zet op `true` om te activeren.")

    # Live stoplicht state (written by scalper_loop every poll_interval_secs)
    state = _q_stoplicht_state(coin)
    if state:
        color = state.get("color", "ROOD")
        direction = state.get("direction")
        score = state.get("score", 0.0)
        updated_at = state.get("updated_at", "")
        color_hex = {"GROEN": "#4ade80", "ORANJE": "#fb923c", "ROOD": "#f87171"}.get(color, "#888")

        col1, col2, col3, col4 = st.columns(4)
        col1.markdown(
            f'<div style="font-size:2em;font-weight:700;color:{color_hex};">{color}</div>',
            unsafe_allow_html=True,
        )
        col2.metric("Richting", direction or "—")
        col3.metric("Score", f"{score:.3f}")
        if updated_at:
            try:
                dt = datetime.fromisoformat(updated_at).astimezone(timezone.utc)
                secs_ago = (datetime.now(timezone.utc) - dt).total_seconds()
                col4.metric("Bijgewerkt", f"{int(secs_ago)}s geleden")
            except Exception:
                col4.metric("Bijgewerkt", updated_at[:19])
    else:
        st.info("Stoplicht nog niet beschikbaar — wacht op eerste scalper evaluatie."
                " Activeer `stoplicht_scalper` mode om de scalper te starten.")

    st.divider()

    # Stats
    stats = _q_stats(coin)
    traded = stats.get("traded") or 0
    wins = stats.get("wins") or 0
    total_pnl = stats.get("total_pnl") or 0.0
    avg_pnl = stats.get("avg_pnl") or 0.0
    win_rate = round(wins / traded * 100, 1) if traded else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Windows gehandeld", traded)
    c2.metric("Win rate", f"{win_rate}%")
    c3.metric("Totaal P&L", f"€{total_pnl:+.4f}")
    c4.metric("Gem P&L", f"€{avg_pnl:+.4f}")

    # Stoplicht distribution
    dist = _q_stoplicht_distribution(coin)
    if dist:
        total_dist = sum(dist.values())
        cols = st.columns(3)
        for i, (c, label) in enumerate([("GROEN", "GROEN"), ("ORANJE", "ORANJE"), ("ROOD", "ROOD")]):
            n = dist.get(c, 0)
            pct = round(n / total_dist * 100, 0) if total_dist else 0
            cols[i].metric(label, f"{n} ({pct:.0f}%)")

    st.divider()

    # Hold threshold info
    from src.stoplicht_scalper import _get_hold_threshold
    threshold = _get_hold_threshold(coin)
    st.caption(f"Hold threshold: {threshold:.2f} (zelf-lerend — bijgesteld op basis van `held_to_end` resultaten)")

    st.divider()

    # Recent windows table
    st.subheader("Recente windows")
    rows = _q_recent_windows(coin, limit=50)
    if not rows:
        st.caption("Nog geen window data — scalper heeft nog geen windows verwerkt.")
        return

    df = pd.DataFrame(rows)

    # Readable column names
    rename = {
        "window_start": "Start",
        "stoplicht": "Licht",
        "direction": "Richting",
        "entry_price": "Entry",
        "exit_price": "Exit",
        "exit_reason": "Reden",
        "pnl_eur": "P&L (€)",
        "hold_threshold_used": "Hold thr.",
        "paper": "Paper",
    }
    show_cols = [c for c in rename if c in df.columns]
    show = df[show_cols].rename(columns=rename).copy()

    # Format
    for col in ["Entry", "Exit", "Hold thr."]:
        if col in show.columns:
            show[col] = show[col].apply(lambda x: f"{x:.3f}" if x is not None and x == x else "—")
    if "P&L (€)" in show.columns:
        show["P&L (€)"] = show["P&L (€)"].apply(
            lambda x: f"€{x:+.4f}" if x is not None and x == x else "—"
        )
    if "Paper" in show.columns:
        show["Paper"] = show["Paper"].apply(lambda x: "ja" if x else "nee")

    try:
        styled = show.style.applymap(_color_pnl, subset=["P&L (€)"])  # type: ignore[attr-defined]
        st.dataframe(styled, hide_index=True, use_container_width=True)
    except Exception:
        st.dataframe(show, hide_index=True, use_container_width=True)
