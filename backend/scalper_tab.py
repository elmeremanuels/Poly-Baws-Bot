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


@st.cache_data(ttl=3)
def _q_stoplicht_state(coin: str) -> dict:
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

    from src.state import get_mode
    current_mode = get_mode()
    is_active = current_mode == "stoplicht_scalper"

    # ── Header ─────────────────────────────────────────────────────────────────
    st.markdown("## 🚦 Stoplicht Scalper")
    st.caption(
        "**Strategie**: directionale 5-minuten scalper. Evalueert OFI + orderboek-imbalans + "
        "VWAP-momentum + perp OFI → kleur GROEN/ORANJE/ROOD. "
        "Stapt in bij GROEN (T-60s voor window), trailing stop + momentum-gate tijdens de window, "
        "houdt vast tot $1.00 als winnende kant ≥ 0.88 in de laatste 90 seconden."
    )
    st.caption(
        "**Verschil met BGGDSB**: BGGDSB koopt beide kanten (straddle, hold to expiry). "
        "De Scalper koopt slechts **één kant** (directional) en heeft een actieve exit-logica."
    )

    st.divider()

    # ── Modus activatie ────────────────────────────────────────────────────────
    if not is_active:
        if current_mode in ("bggdsb_paper", "bggdsb_live"):
            st.warning(
                f"⚠️ Bot draait nu in **{current_mode}**. Schakel naar "
                "`stoplicht_scalper` via de ⚙️ Instellingen tab of de sidebar.",
                icon="🔄",
            )
        else:
            st.info(
                f"ℹ️ Bot draait nu in **{current_mode}**. "
                "Schakel naar `stoplicht_scalper` via de sidebar om te starten met traden. "
                "Het stoplicht hieronder wordt altijd live bijgehouden.",
            )

    mode_col, _ = st.columns([2, 3])
    with mode_col:
        if not is_active:
            if st.button("▶ Activeer Stoplicht Scalper", type="primary", key="sc_activate"):
                try:
                    from src.commands import write_command
                    write_command("set_mode", {"mode": "stoplicht_scalper"})
                    st.success("Modus ingesteld op stoplicht_scalper.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
        else:
            st.success(f"✅ Stoplicht Scalper actief — {coin} {'(paper)' if paper else '(LIVE)'}")
            if st.button("⏸ Pauzeer (terug naar paper_hybrid)", key="sc_deactivate"):
                try:
                    from src.commands import write_command
                    write_command("set_mode", {"mode": "paper_hybrid"})
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    st.divider()

    # ── Live stoplicht indicator ────────────────────────────────────────────────
    st.markdown(f"### Live stoplicht — {coin}")

    state = _q_stoplicht_state(coin)
    if state:
        color = state.get("color", "ROOD")
        direction = state.get("direction")
        score = state.get("score", 0.0)
        updated_at = state.get("updated_at", "")

        color_hex = {"GROEN": "#4ade80", "ORANJE": "#fb923c", "ROOD": "#f87171"}.get(color, "#888")
        color_bg  = {"GROEN": "#0d2b0d", "ORANJE": "#2b1a0d", "ROOD": "#2b0d0d"}.get(color, "#1a1a1a")
        verdict   = {"GROEN": "✅ Instap mogelijk", "ORANJE": "⏳ Afwachten", "ROOD": "🚫 Geen entry"}.get(color, "—")

        st.markdown(
            f'<div style="background:{color_bg};border:2px solid {color_hex};border-radius:12px;'
            f'padding:20px 28px;margin-bottom:12px;">'
            f'<span style="font-size:3em;font-weight:800;color:{color_hex};">{color}</span>'
            f'<span style="font-size:1.1em;color:#94a3b8;margin-left:20px;">{verdict}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Richting", direction or "—")
        c2.metric("Score", f"{score:.3f}")
        c3.metric("Drempel GROEN", f"{cfg.get('green_threshold', 0.60):.2f}")
        if updated_at:
            try:
                dt = datetime.fromisoformat(updated_at).astimezone(timezone.utc)
                secs_ago = (datetime.now(timezone.utc) - dt).total_seconds()
                c4.metric("Bijgewerkt", f"{int(secs_ago)}s geleden")
            except Exception:
                c4.metric("Bijgewerkt", updated_at[:19])

        # Score breakdown bar
        bar_pct = min(100, int(score * 100))
        bar_color = color_hex
        st.markdown(
            f'<div style="background:#1e2330;border-radius:6px;height:10px;margin:4px 0 12px 0;">'
            f'<div style="background:{bar_color};width:{bar_pct}%;height:10px;border-radius:6px;"></div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        st.caption(f"Score {score:.3f} / 1.000 — wegingen: OFI 35% · OBI 28% · MOM 20% · Perp 12% · CVD 5%")
    else:
        st.markdown(
            '<div style="background:#1e2330;border:1px solid #334155;border-radius:12px;'
            'padding:20px 28px;color:#64748b;font-size:1.1em;">'
            '⏳ Stoplicht wordt geladen — bot evalueert iedere ~10s…'
            '</div>',
            unsafe_allow_html=True,
        )

    st.divider()

    # ── Statistieken ───────────────────────────────────────────────────────────
    st.markdown("### Statistieken")
    stats = _q_stats(coin)
    traded = stats.get("traded") or 0
    wins = stats.get("wins") or 0
    total_pnl = stats.get("total_pnl") or 0.0
    avg_pnl = stats.get("avg_pnl") or 0.0
    win_rate = round(wins / traded * 100, 1) if traded else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Windows gehandeld", traded)
    c2.metric("Win rate", f"{win_rate:.1f}%")
    c3.metric("Totaal P&L", f"€{total_pnl:+.4f}")
    c4.metric("Gem. P&L/trade", f"€{avg_pnl:+.4f}" if avg_pnl else "—")

    # Stoplicht-kleur verdeling
    dist = _q_stoplicht_distribution(coin)
    if dist:
        total_dist = sum(dist.values())
        st.caption("Verdeling van stoplicht-kleur over alle geëvalueerde windows:")
        dc1, dc2, dc3 = st.columns(3)
        for col_obj, (key, label, hex_c) in zip(
            [dc1, dc2, dc3],
            [("GROEN", "🟢 GROEN", "#4ade80"), ("ORANJE", "🟠 ORANJE", "#fb923c"), ("ROOD", "🔴 ROOD", "#f87171")],
        ):
            n = dist.get(key, 0)
            pct = round(n / total_dist * 100, 0) if total_dist else 0
            col_obj.markdown(
                f'<div style="background:#1e2330;border-radius:8px;padding:12px;text-align:center;">'
                f'<div style="color:{hex_c};font-size:1.5em;font-weight:700;">{n}</div>'
                f'<div style="color:#94a3b8;font-size:0.8em;">{label} · {pct:.0f}%</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.divider()

    # ── Hold threshold ─────────────────────────────────────────────────────────
    from src.stoplicht_scalper import _get_hold_threshold
    threshold = _get_hold_threshold(coin)
    st.markdown("### Exit-parameters")
    ec1, ec2, ec3 = st.columns(3)
    ec1.metric("Hold drempel (mid)", f"{threshold:.2f}",
               help="Als winnende kant ≥ deze waarde in fase 2 → hold tot $1 resolutie")
    ec2.metric("Trail activatie", f"{cfg.get('trail_activate_cts', 5)}¢")
    ec3.metric("Trail buffer", f"{cfg.get('trail_buffer_cts', 2)}¢")
    st.caption("Hold drempel is zelf-lerend: daalt bij hoog held-to-end succespercentage, stijgt bij laag.")

    st.divider()

    # ── Recente windows ────────────────────────────────────────────────────────
    st.markdown("### Recente windows")
    rows = _q_recent_windows(coin, limit=50)
    if not rows:
        st.caption("Nog geen window data — de scalper registreert hier elke window zodra hij actief is.")
        return

    df = pd.DataFrame(rows)
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
        styled = show.style.map(_color_pnl, subset=["P&L (€)"])
        st.dataframe(styled, hide_index=True, use_container_width=True)
    except Exception:
        st.dataframe(show, hide_index=True, use_container_width=True)
