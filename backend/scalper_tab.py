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


def _db_state(key: str) -> str | None:
    import sqlite3
    try:
        con = sqlite3.connect(str(_get_db_path()))
        row = con.execute(
            "SELECT value FROM dashboard_state WHERE key = ?", (key,)
        ).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


@st.cache_data(ttl=3)
def _q_stoplicht_state(coin: str) -> dict:
    raw = _db_state(f"scalper_stoplicht_{coin}")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {}


@st.cache_data(ttl=2)
def _q_window_state(coin: str) -> dict:
    raw = _db_state(f"scalper_window_{coin}")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {}


@st.cache_data(ttl=10)
def _q_portfolio() -> float:
    raw = _db_state("portfolio_usdc")
    try:
        return float(raw) if raw else 0.0
    except Exception:
        return 0.0


@st.cache_data(ttl=5)
def _q_recent_windows(coin: str, limit: int = 30) -> list[dict]:
    import sqlite3
    try:
        con = sqlite3.connect(str(_get_db_path()))
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT window_id, coin, window_start, stoplicht, direction,
                   entry_price, exit_price, exit_reason, pnl_eur,
                   trades_in_window, hold_threshold_used, paper, created_at
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
                   SUM(CASE WHEN trades_in_window > 0 THEN 1 ELSE 0 END) AS traded,
                   SUM(CASE WHEN pnl_eur > 0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(SUM(pnl_eur), 4) AS total_pnl,
                   ROUND(AVG(CASE WHEN trades_in_window > 0 THEN pnl_eur END), 4) AS avg_pnl
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


def _fmt_mid(v) -> str:
    if v is None:
        return "—"
    return f"{v:.3f}"


def scalper_panel() -> None:
    cfg = CONFIG.get("stoplicht_scalper", {})
    coin = cfg.get("coin", "BTC")
    paper = cfg.get("paper_mode", True)

    from src.state import get_mode
    current_mode = get_mode()
    is_active = current_mode == "stoplicht_scalper"

    # ── Header ─────────────────────────────────────────────────────────────────
    st.markdown("## 🚦 Stoplicht Scalper")
    st.caption(
        "**Strategie**: directionale 15-minuten scalper. OFI + OBI + MOM + Perp OFI + CVD + "
        "S/R walls → GROEN/ORANJE/ROOD. Entree op GROEN + Polymarket bevestiging. "
        "Trailing stop (5¢ activate / 2¢ buffer), MOM reversal exit, contrarian hedge bij book wall. "
        "Phase 2 (laatste 3 min): hold als winnende kant ≥ 0.88, anders finale positie."
    )

    st.divider()

    # ── Modus activatie + portfolio ────────────────────────────────────────────
    act_col, port_col = st.columns([3, 2])
    with act_col:
        if not is_active:
            if current_mode in ("bggdsb_paper", "bggdsb_live"):
                st.warning(f"⚠️ Bot draait nu in **{current_mode}**.", icon="🔄")
            else:
                st.info(f"ℹ️ Bot draait nu in **{current_mode}**. Schakel via de sidebar.")
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

    with port_col:
        usdc = _q_portfolio()
        if usdc > 0:
            from src.stoplicht_scalper import _get_hold_threshold
            sizing = cfg.get("sizing", {})
            brackets = max(1, int(usdc // 100))
            main_eur = brackets * float(sizing.get("main_pct_per_100", 5.0))
            hedge_eur = brackets * float(sizing.get("hedge_pct_per_100", 1.0))
            st.metric("Portfolio USDC", f"${usdc:,.2f}",
                      help=f"Sizing: €{main_eur:.0f} main / €{hedge_eur:.0f} hedge per window")
        else:
            st.metric("Portfolio USDC", "—")

    st.divider()

    # ── Huidig window ──────────────────────────────────────────────────────────
    st.markdown(f"### Huidig window — {coin}")
    ws = _q_window_state(coin)
    secs_left = ws.get("secs_left", 0)

    if secs_left and secs_left > 0:
        window_end_str = ws.get("window_end", "")
        phase2 = ws.get("phase2", False)
        yes_mid = ws.get("yes_mid")
        no_mid  = ws.get("no_mid")
        has_pos = ws.get("has_position", False)
        pos_dir = ws.get("position_direction")
        pos_entry = ws.get("position_entry")
        pos_peak  = ws.get("position_peak")
        trailing  = ws.get("trailing_active", False)
        trades_n  = ws.get("trades_in_window", 0)
        running_pnl = ws.get("running_pnl", 0.0)

        phase_label = "🟡 Phase 2 — anticipatie" if phase2 else "🟢 Phase 1 — actief"
        total_secs = cfg.get("phase2_secs", 180) + (secs_left if not phase2 else 0)
        window_dur = 15 * 60
        pct_done = max(0, min(100, int((window_dur - secs_left) / window_dur * 100)))

        mins = secs_left // 60
        secs = secs_left % 60
        st.markdown(
            f'<div style="background:#1e2330;border-radius:8px;padding:12px 16px;margin-bottom:8px;">'
            f'<span style="font-size:1.1em;font-weight:700;color:#e2e8f0;">'
            f'⏱ {mins}:{secs:02d} resterend</span>'
            f'<span style="font-size:0.85em;color:#64748b;margin-left:12px;">{phase_label}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
        st.progress(pct_done, text=f"{pct_done}% verstreken")

        # YES / NO prices
        w1, w2, w3, w4 = st.columns(4)
        yes_color = "#4ade80" if (yes_mid and yes_mid > 0.50) else "#f87171"
        no_color  = "#4ade80" if (no_mid  and no_mid  > 0.50) else "#f87171"
        w1.metric("YES mid", _fmt_mid(yes_mid),
                  help="Huidige mid-prijs van het YES-token op Polymarket")
        w2.metric("NO mid", _fmt_mid(no_mid),
                  help="Huidige mid-prijs van het NO-token op Polymarket")

        if has_pos and pos_entry:
            pos_pnl_pct = ((pos_peak or pos_entry) - pos_entry) / pos_entry * 100 if pos_entry else 0
            w3.metric(
                f"Positie ({pos_dir})",
                f"{pos_entry:.3f}",
                delta=f"piek {pos_peak:.3f}" if pos_peak else None,
            )
            w4.metric(
                "Trailing",
                "✅ Actief" if trailing else "⏳ Wacht",
                help="Trailing stop activeert bij +5¢ boven entry",
            )
        else:
            w3.metric("Positie", "— geen")
            w4.metric("Trades", trades_n, help="Trades in dit window tot nu toe")

        if running_pnl != 0.0:
            pnl_col = "#4ade80" if running_pnl > 0 else "#f87171"
            st.markdown(
                f'<div style="font-size:0.9em;color:{pnl_col};margin-top:4px;">'
                f'Lopend P&L: €{running_pnl:+.4f} ({trades_n} trades)'
                f'</div>',
                unsafe_allow_html=True,
            )
    else:
        st.markdown(
            '<div style="background:#1e2330;border:1px solid #334155;border-radius:8px;'
            'padding:12px 16px;color:#64748b;">'
            '⏳ Geen actief window — scalper wacht op de volgende 15m markt.'
            '</div>',
            unsafe_allow_html=True,
        )

    st.divider()

    # ── Live stoplicht indicator ────────────────────────────────────────────────
    st.markdown(f"### Live stoplicht — {coin}")

    state = _q_stoplicht_state(coin)
    if state:
        color      = state.get("color", "ROOD")
        direction  = state.get("direction")
        score      = state.get("score", 0.0)
        updated_at = state.get("updated_at", "")
        regime     = state.get("regime", "")
        ofi        = state.get("ofi")
        obi        = state.get("obi")
        mom        = state.get("mom")
        cvd        = state.get("cvd")
        cur_price  = state.get("current_price")
        supports    = state.get("supports", [])
        resistances = state.get("resistances", [])

        color_hex = {"GROEN": "#4ade80", "ORANJE": "#fb923c", "ROOD": "#f87171"}.get(color, "#888")
        color_bg  = {"GROEN": "#0d2b0d", "ORANJE": "#2b1a0d", "ROOD": "#2b0d0d"}.get(color, "#1a1a1a")
        verdict   = {"GROEN": "✅ Instap mogelijk", "ORANJE": "⏳ Afwachten", "ROOD": "🚫 Geen entry"}.get(color, "—")
        regime_color = {"RANGING": "#4ade80", "TRENDING": "#60a5fa", "CHOPPY": "#f87171"}.get(regime, "#94a3b8")

        st.markdown(
            f'<div style="background:{color_bg};border:2px solid {color_hex};border-radius:12px;'
            f'padding:16px 24px;margin-bottom:12px;display:flex;align-items:center;gap:16px;">'
            f'<span style="font-size:2.8em;font-weight:800;color:{color_hex};">{color}</span>'
            f'<span style="font-size:1em;color:#94a3b8;">{verdict}</span>'
            + (f'<span style="font-size:0.85em;color:{regime_color};'
               f'background:{regime_color}22;padding:2px 10px;border-radius:4px;">{regime}</span>'
               if regime else "")
            + (f'<span style="font-size:0.85em;color:#64748b;margin-left:auto;">'
               f'BTC ${cur_price:,.0f}</span>' if cur_price else "")
            + f'</div>',
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

        bar_pct = min(100, int(score * 100))
        st.markdown(
            f'<div style="background:#1e2330;border-radius:6px;height:8px;margin:4px 0 10px 0;">'
            f'<div style="background:{color_hex};width:{bar_pct}%;height:8px;border-radius:6px;"></div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # Signal breakdown
        def _fmt(v):
            return f"{v:.3f}" if v is not None else "—"

        sc1, sc2, sc3, sc4 = st.columns(4)
        sc1.metric("OFI", _fmt(ofi), help="Order Flow Imbalance spot (35%)")
        sc2.metric("OBI", _fmt(obi), help="Order Book Imbalance top-10 (28%)")
        sc3.metric("MOM", _fmt(mom), help="VWAP momentum 45s (20%)")
        sc4.metric("CVD", _fmt(cvd), help="CVD slope acceleratie (5%)")

        # S/R levels — mirroring indicator_app layout
        if cur_price and (supports or resistances):
            st.markdown("**S/R niveaus** (live book walls · dagelijkse OHLC pivots · VPOC nodes)")
            src_color = {"wall": "#f87171", "vol": "#a78bfa", "gist.H": "#60a5fa",
                         "gist.L": "#60a5fa", "dag H": "#34d399", "dag L": "#34d399"}

            rows_html = []
            for lvl in reversed(resistances[:2]):
                clr = src_color.get(lvl["tag"], "#ef4444")
                rows_html.append(
                    f'<tr>'
                    f'<td style="color:#ef4444;font-family:monospace;padding:2px 6px;">${lvl["price"]:,.0f}</td>'
                    f'<td style="color:{clr};font-size:10px;padding:2px 4px;">{lvl["tag"]}</td>'
                    f'<td style="color:#ef4444;text-align:right;padding:2px 6px;">+{lvl["pct"]:.2f}%</td>'
                    f'</tr>'
                )
            rows_html.append(
                f'<tr style="background:#2a2a3a;">'
                f'<td style="color:#e2e8f0;font-family:monospace;font-weight:700;padding:3px 6px;">${cur_price:,.0f}</td>'
                f'<td style="color:#64748b;font-size:10px;padding:3px 4px;">nu</td>'
                f'<td></td></tr>'
            )
            for lvl in supports[:2]:
                clr = src_color.get(lvl["tag"], "#22c55e")
                rows_html.append(
                    f'<tr>'
                    f'<td style="color:#4ade80;font-family:monospace;padding:2px 6px;">${lvl["price"]:,.0f}</td>'
                    f'<td style="color:{clr};font-size:10px;padding:2px 4px;">{lvl["tag"]}</td>'
                    f'<td style="color:#4ade80;text-align:right;padding:2px 6px;">-{lvl["pct"]:.2f}%</td>'
                    f'</tr>'
                )
            st.markdown(
                f'<table style="width:100%;border-collapse:collapse;font-size:13px;">'
                + "".join(rows_html) + "</table>",
                unsafe_allow_html=True,
            )
        elif not supports and not resistances:
            st.caption("S/R niveaus worden geladen (engine warmt ±2 min op voor OHLC pivots).")

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
    c4.metric("Gem. P&L/window", f"€{avg_pnl:+.4f}" if avg_pnl else "—")

    dist = _q_stoplicht_distribution(coin)
    if dist:
        total_dist = sum(dist.values())
        st.caption("Verdeling stoplicht-kleur over geëvalueerde windows:")
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

    # ── Exit-parameters ────────────────────────────────────────────────────────
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
        "trades_in_window": "#",
        "pnl_eur": "P&L (€)",
        "paper": "Paper",
    }
    show_cols = [c for c in rename if c in df.columns]
    show = df[show_cols].rename(columns=rename).copy()

    for col in ["Entry", "Exit"]:
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
