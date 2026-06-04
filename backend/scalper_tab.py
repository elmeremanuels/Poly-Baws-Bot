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


@st.cache_data(ttl=2)
def _q_stoplicht_state(coin: str) -> dict:
    raw = _db_state(f"scalper_stoplicht_{coin}")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {}


@st.cache_data(ttl=1)
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
            SELECT window_start, stoplicht, direction,
                   entry_price, exit_price, exit_reason, pnl_eur,
                   trades_in_window, paper
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
                   SUM(CASE WHEN pnl_eur > 0 AND trades_in_window > 0 THEN 1 ELSE 0 END) AS wins,
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
def _q_dist(coin: str) -> dict:
    import sqlite3
    try:
        con = sqlite3.connect(str(_get_db_path()))
        rows = con.execute(
            "SELECT stoplicht, COUNT(*) FROM window_tradelog WHERE coin=? GROUP BY stoplicht",
            (coin,),
        ).fetchall()
        con.close()
        return {r[0]: r[1] for r in rows if r[0]}
    except Exception:
        return {}


# ── HTML helpers ────────────────────────────────────────────────────────────────

def _bar(val: float | None) -> str:
    """5-block bar, matching indicator_app style."""
    if val is None:
        return '<span style="color:#555;font-family:monospace">—</span>'
    frac = max(0.0, min(1.0, val))
    filled = round(frac * 5)
    clr = "#22c55e"
    return f'<span style="color:{clr};font-family:monospace">{"█"*filled}{"░"*(5-filled)}</span>'


def _mom(mom: float | None) -> str:
    if mom is None:
        return '<span style="color:#555">—</span>'
    clr = "#22c55e" if mom > 0 else "#ef4444"
    arrow = "▲" if mom > 0 else "▼"
    return f'<span style="color:{clr}">{arrow} {abs(mom):.3f}</span>'


def _cvd(cvd: float | None) -> str:
    if cvd is None:
        return '<span style="color:#555">—</span>'
    clr = "#22c55e" if cvd >= 0 else "#ef4444"
    return f'<span style="color:{clr}">{cvd:+.3f}</span>'


def _signals_html(ofi, obi, mom_v, cvd_v) -> str:
    return (
        '<table style="width:100%;font-size:12px;border-collapse:collapse;line-height:1.6;">'
        f'<tr><td style="color:#64748b;padding:2px 4px;width:30px;">OFI</td>'
        f'<td style="padding:2px 4px;">{_bar(ofi)}</td>'
        f'<td style="color:#475569;font-family:monospace;text-align:right;padding:2px 4px;">{f"{ofi:.3f}" if ofi is not None else "—"}</td>'
        f'<td style="color:#334155;font-size:10px;text-align:right;padding:2px 4px;">35%</td></tr>'
        f'<tr><td style="color:#64748b;padding:2px 4px;">OBI</td>'
        f'<td style="padding:2px 4px;">{_bar(obi)}</td>'
        f'<td style="color:#475569;font-family:monospace;text-align:right;padding:2px 4px;">{f"{obi:.3f}" if obi is not None else "—"}</td>'
        f'<td style="color:#334155;font-size:10px;text-align:right;padding:2px 4px;">28%</td></tr>'
        f'<tr><td style="color:#64748b;padding:2px 4px;">MOM</td>'
        f'<td colspan="2" style="padding:2px 4px;">{_mom(mom_v)}</td>'
        f'<td style="color:#334155;font-size:10px;text-align:right;padding:2px 4px;">20%</td></tr>'
        f'<tr><td style="color:#64748b;padding:2px 4px;">CVD</td>'
        f'<td colspan="2" style="padding:2px 4px;">{_cvd(cvd_v)}</td>'
        f'<td style="color:#334155;font-size:10px;text-align:right;padding:2px 4px;">5%</td></tr>'
        '</table>'
    )


def _sr_html(cur_price: float, supports: list, resistances: list) -> str:
    """S/R levels table identical to indicator_app layout."""
    src_clr = {"wall": "#ef4444", "vol": "#a78bfa"}  # OHLC tags → #60a5fa
    rows = []

    for lvl in reversed(resistances[:3]):
        tag = lvl.get("tag", "?")
        clr = src_clr.get(tag, "#60a5fa")
        rows.append(
            f'<tr>'
            f'<td style="color:#ef4444;font-family:monospace;padding:1px 4px;">${lvl["price"]:,.0f}</td>'
            f'<td style="color:{clr};font-size:10px;padding:1px 3px;">{tag}</td>'
            f'<td style="color:#ef4444;text-align:right;padding:1px 4px;">+{lvl["pct"]:.1f}%</td>'
            f'</tr>'
        )

    rows.append(
        f'<tr style="background:#222;border-top:1px solid #444;border-bottom:1px solid #444;">'
        f'<td style="color:#e0e0e0;font-family:monospace;font-weight:700;padding:2px 4px;">'
        f'${cur_price:,.0f}</td>'
        f'<td style="color:#888;font-size:10px;padding:2px 3px;">nu</td>'
        f'<td></td></tr>'
    )

    for lvl in supports[:3]:
        tag = lvl.get("tag", "?")
        clr = src_clr.get(tag, "#60a5fa")
        rows.append(
            f'<tr>'
            f'<td style="color:#22c55e;font-family:monospace;padding:1px 4px;">${lvl["price"]:,.0f}</td>'
            f'<td style="color:{clr};font-size:10px;padding:1px 3px;">{tag}</td>'
            f'<td style="color:#22c55e;text-align:right;padding:1px 4px;">-{lvl["pct"]:.1f}%</td>'
            f'</tr>'
        )

    return (
        '<table style="width:100%;border-collapse:collapse;font-size:11px;">'
        + "".join(rows) + '</table>'
    )


def _color_pnl(val) -> str:
    try:
        v = float(val)
        if v > 0:   return "color:#4ade80"
        if v < 0:   return "color:#f87171"
    except Exception:
        pass
    return ""


# ── Main panel ──────────────────────────────────────────────────────────────────

@st.fragment(run_every=2)
def scalper_panel() -> None:
    cfg  = CONFIG.get("stoplicht_scalper", {})
    coin = cfg.get("coin", "BTC")
    paper = cfg.get("paper_mode", True)

    from src.db_sync import get_state as _get_state
    current_mode = _get_state("mode") or "paper_hybrid"
    is_active = current_mode == "stoplicht_scalper"

    st.markdown("## 🚦 Stoplicht Scalper")

    # ── Mode + Portfolio row ────────────────────────────────────────────────────
    act_col, port_col = st.columns([3, 2])
    with act_col:
        if not is_active:
            st.info(f"Bot draait in **{current_mode}**.", icon="ℹ️")
            if st.button("▶ Activeer Stoplicht Scalper", type="primary", key="sc_activate"):
                try:
                    from src.commands import write_command
                    write_command("set_mode", {"mode": "stoplicht_scalper"})
                    st.success("Modus → stoplicht_scalper")
                    st.rerun(scope="app")
                except Exception as e:
                    st.error(str(e))
        else:
            st.success(f"✅ Actief — {coin} {'📄 paper' if paper else '💶 LIVE'}")
            if st.button("⏸ Pauzeer", key="sc_deactivate"):
                try:
                    from src.commands import write_command
                    write_command("set_mode", {"mode": "paper_hybrid"})
                    st.rerun(scope="app")
                except Exception as e:
                    st.error(str(e))

    with port_col:
        usdc = _q_portfolio()
        if usdc > 0:
            sizing = cfg.get("sizing", {})
            brackets = max(1, int(usdc // 100))
            main_eur = brackets * float(sizing.get("main_pct_per_100", 5.0))
            hedge_eur = brackets * float(sizing.get("hedge_pct_per_100", 1.0))
            st.metric("Portfolio", f"${usdc:,.2f}",
                      help=f"Sizing: €{main_eur:.0f} main / €{hedge_eur:.0f} hedge per window")
        else:
            st.metric("Portfolio", "—")

    st.divider()

    # ── Live Indicator card ─────────────────────────────────────────────────────
    state = _q_stoplicht_state(coin)

    if state:
        color      = state.get("color", "ROOD")
        direction  = state.get("direction")          # "YES"/"NO"/None
        score      = state.get("score", 0.0)
        updated_at = state.get("updated_at", "")
        regime     = state.get("regime", "UNKNOWN")
        ofi        = state.get("ofi")
        obi        = state.get("obi")
        mom_v      = state.get("mom")
        cvd_v      = state.get("cvd")
        cur_price  = state.get("current_price")
        supports    = state.get("supports", [])
        resistances = state.get("resistances", [])

        clr_map = {"GROEN": "#22c55e", "ORANJE": "#f97316", "ROOD": "#ef4444"}
        bg_map  = {"GROEN": "#052e16", "ORANJE": "#431407", "ROOD": "#450a0a"}
        color_hex = clr_map.get(color, "#888")
        color_bg  = bg_map.get(color, "#1a1a1a")

        dir_map  = {"YES": ("▲ STIJGING", "#22c55e"), "NO": ("▼ DALING", "#ef4444")}
        dir_lbl, dir_clr = dir_map.get(direction, ("◆ UNDECIDED", "#f97316"))

        regime_clr = {"RANGING": "#22c55e", "TRENDING": "#3b82f6",
                      "CHOPPY": "#ef4444"}.get(regime, "#94a3b8")

        verdict = {"GROEN": "✅ Instap mogelijk",
                   "ORANJE": "⏳ Afwachten",
                   "ROOD": "🚫 Geen entry"}.get(color, "—")

        age_str = ""
        if updated_at:
            try:
                dt = datetime.fromisoformat(updated_at).astimezone(timezone.utc)
                secs_ago = int((datetime.now(timezone.utc) - dt).total_seconds())
                age_str = f"⟳ {secs_ago}s geleden"
            except Exception:
                pass

        price_str = f"BTC ${cur_price:,.0f}" if cur_price else ""

        # Colored header
        st.markdown(
            f'<div style="background:{color_bg};border:2px solid {color_hex};border-radius:10px;'
            f'padding:10px 16px;margin-bottom:8px;">'
            f'<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">'
            f'<span style="font-size:1.9em;font-weight:800;color:{color_hex};">{color}</span>'
            f'<span style="font-size:1.05em;font-weight:700;color:{dir_clr};">{dir_lbl}</span>'
            f'<span style="color:#94a3b8;font-size:0.85em;">{verdict}</span>'
            + (f'<span style="font-size:0.78em;color:{regime_clr};background:{regime_clr}20;'
               f'padding:2px 8px;border-radius:4px;font-weight:600;">{regime}</span>' if regime else "")
            + f'<span style="font-size:0.78em;color:#475569;margin-left:auto;">'
            f'score {score:.3f} &nbsp;·&nbsp; {price_str} &nbsp;·&nbsp; {age_str}</span>'
            f'</div></div>',
            unsafe_allow_html=True,
        )

        # Score bar
        bar_w = min(100, int(score * 100))
        st.markdown(
            f'<div style="background:#1e2330;border-radius:4px;height:5px;margin:0 0 10px 0;">'
            f'<div style="background:{color_hex};width:{bar_w}%;height:5px;border-radius:4px;"></div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # Signals left / S&R right
        sig_col, sr_col = st.columns(2)

        with sig_col:
            st.markdown(
                f'<div style="background:#0f172a;border-radius:8px;padding:10px 14px;">'
                + _signals_html(ofi, obi, mom_v, cvd_v)
                + '</div>',
                unsafe_allow_html=True,
            )

        with sr_col:
            if cur_price and (supports or resistances):
                st.markdown(
                    f'<div style="background:#0f172a;border-radius:8px;padding:10px 14px;">'
                    + _sr_html(cur_price, supports, resistances)
                    + '</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<div style="background:#0f172a;border-radius:8px;padding:10px 14px;'
                    'color:#334155;font-size:11px;">S/R laden (±2 min…)</div>',
                    unsafe_allow_html=True,
                )

    else:
        st.markdown(
            '<div style="background:#1e2330;border:1px solid #334155;border-radius:10px;'
            'padding:18px 24px;color:#64748b;">⏳ Stoplicht laadt — bot evalueert iedere ~10s…</div>',
            unsafe_allow_html=True,
        )

    st.divider()

    # ── Huidig window ──────────────────────────────────────────────────────────
    ws        = _q_window_state(coin)
    secs_left = ws.get("secs_left", 0)

    if secs_left and secs_left > 0:
        phase2      = ws.get("phase2", False)
        yes_mid     = ws.get("yes_mid")
        no_mid      = ws.get("no_mid")
        has_pos     = ws.get("has_position", False)
        pos_dir     = ws.get("position_direction")
        pos_entry   = ws.get("position_entry")
        pos_peak    = ws.get("position_peak")
        pos_size    = ws.get("position_size_eur")
        trailing    = ws.get("trailing_active", False)
        trades_n    = ws.get("trades_in_window", 0)
        running_pnl = ws.get("running_pnl", 0.0)

        phase_lbl = "🟡 Phase 2 — anticipatie" if phase2 else "🟢 Phase 1 — actief"
        window_dur = 15 * 60
        pct_done   = max(0, min(100, int((window_dur - secs_left) / window_dur * 100)))
        mins, secs_rem = divmod(int(secs_left), 60)

        st.markdown(f"### 🔴 Actief window — {coin}")
        st.markdown(
            f'<div style="background:#1e2330;border-radius:8px;padding:10px 16px;margin-bottom:6px;'
            f'display:flex;align-items:center;gap:12px;">'
            f'<span style="font-size:1.3em;font-weight:700;color:#e2e8f0;">⏱ {mins}:{secs_rem:02d}</span>'
            f'<span style="color:#94a3b8;font-size:0.85em;"> resterend</span>'
            f'<span style="font-size:0.8em;color:#64748b;margin-left:auto;">{phase_lbl}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
        st.progress(pct_done, text=f"{pct_done}% verstreken")

        # Live prices
        p1, p2, p3, p4 = st.columns(4)
        yes_fav = yes_mid and yes_mid > 0.50
        no_fav  = no_mid  and no_mid  > 0.50
        p1.metric("YES mid", f"{yes_mid:.3f}" if yes_mid else "—",
                  delta="favoriet ✓" if yes_fav else None,
                  delta_color="normal")
        p2.metric("NO mid",  f"{no_mid:.3f}"  if no_mid  else "—",
                  delta="favoriet ✓" if no_fav  else None,
                  delta_color="normal")

        if has_pos and pos_entry:
            cur_mid = yes_mid if pos_dir == "UP" else (no_mid if pos_dir == "DOWN" else None)
            delta_str = None
            if cur_mid and pos_entry:
                chg = cur_mid - pos_entry
                chg_pct = chg / pos_entry * 100
                delta_str = f"{chg:+.3f} ({chg_pct:+.1f}%)"

            dir_arrow = "▲" if pos_dir == "UP" else "▼"
            p3.metric(f"{dir_arrow} {pos_dir or '—'}", f"entry {pos_entry:.3f}",
                      delta=delta_str)
            trail_help = (f"Piek: {pos_peak:.3f}" if pos_peak else "") + (
                " — trailing actief" if trailing else " — wacht op +5¢")
            p4.metric("Trailing", "✅ Actief" if trailing else "⏳ Wacht",
                      help=trail_help)
        else:
            p3.metric("Positie", "— geen")
            p4.metric("Trades (gesloten)", str(trades_n))

        # Unrealized P&L for open position
        unrealized_html = ""
        if has_pos and pos_entry and pos_size and pos_dir:
            cur_mid = yes_mid if pos_dir == "UP" else (no_mid if pos_dir == "DOWN" else None)
            if cur_mid:
                shares = pos_size / pos_entry
                unreal = (cur_mid - pos_entry) * shares
                unreal_clr = "#4ade80" if unreal >= 0 else "#f87171"
                unrealized_html = (
                    f'<span style="color:{unreal_clr};">Open positie: €{unreal:+.4f}</span> &nbsp;·&nbsp; '
                )

        # Running (realized) P&L
        if trades_n > 0 or running_pnl != 0.0:
            pnl_clr = "#4ade80" if running_pnl >= 0 else "#f87171"
            status = "💚 WINST" if running_pnl > 0 else ("💔 VERLIES" if running_pnl < 0 else "±0")
            st.markdown(
                f'<div style="background:#0f172a;border-radius:6px;padding:8px 14px;margin-top:6px;">'
                + unrealized_html
                + f'<span style="color:{pnl_clr};font-weight:700;">Gerealiseerd: €{running_pnl:+.4f}</span>'
                f' <span style="color:#64748b;font-size:0.85em;"> {status} · {trades_n} trades</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
        elif unrealized_html:
            cur_mid = yes_mid if pos_dir == "UP" else no_mid
            if cur_mid:
                shares = (pos_size or 0) / pos_entry
                unreal = (cur_mid - pos_entry) * shares
                pnl_clr = "#4ade80" if unreal >= 0 else "#f87171"
                st.markdown(
                    f'<div style="background:#0f172a;border-radius:6px;padding:8px 14px;margin-top:6px;">'
                    f'<span style="color:{pnl_clr};font-weight:700;">Open positie: €{unreal:+.4f}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    else:
        st.markdown(f"### ⏳ Huidig window — {coin}")
        st.markdown(
            '<div style="background:#1e2330;border:1px solid #334155;border-radius:8px;'
            'padding:12px 16px;color:#64748b;">'
            'Geen actief window — scalper wacht op de volgende 15-minuten markt.'
            '</div>',
            unsafe_allow_html=True,
        )

    st.divider()

    # ── Statistieken ───────────────────────────────────────────────────────────
    st.markdown("### Statistieken")
    stats  = _q_stats(coin)
    traded = stats.get("traded") or 0
    wins   = stats.get("wins") or 0
    total_pnl = stats.get("total_pnl") or 0.0
    avg_pnl   = stats.get("avg_pnl") or 0.0
    win_rate  = round(wins / traded * 100, 1) if traded else 0.0

    sc1, sc2, sc3, sc4 = st.columns(4)
    sc1.metric("Windows gehandeld", traded)
    sc2.metric("Win rate", f"{win_rate:.1f}%")
    sc3.metric("Totaal P&L", f"€{total_pnl:+.4f}")
    sc4.metric("Gem. P&L/window", f"€{avg_pnl:+.4f}" if avg_pnl else "—")

    dist = _q_dist(coin)
    if dist:
        total_dist = sum(dist.values())
        st.caption("Stoplicht verdeling over alle windows:")
        dc1, dc2, dc3 = st.columns(3)
        for col_obj, (key, lbl, hex_c) in zip(
            [dc1, dc2, dc3],
            [("GROEN", "🟢 GROEN", "#4ade80"),
             ("ORANJE", "🟠 ORANJE", "#fb923c"),
             ("ROOD", "🔴 ROOD", "#f87171")],
        ):
            n   = dist.get(key, 0)
            pct = round(n / total_dist * 100) if total_dist else 0
            col_obj.markdown(
                f'<div style="background:#1e2330;border-radius:8px;padding:10px;text-align:center;">'
                f'<div style="color:{hex_c};font-size:1.4em;font-weight:700;">{n}</div>'
                f'<div style="color:#94a3b8;font-size:0.75em;">{lbl} · {pct}%</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.divider()

    # ── Exit-parameters ────────────────────────────────────────────────────────
    from src.stoplicht_scalper import _get_hold_threshold
    threshold = _get_hold_threshold(coin)
    st.markdown("### Exit-parameters")
    ep1, ep2, ep3 = st.columns(3)
    ep1.metric("Hold drempel", f"{threshold:.2f}",
               help="Winnende kant ≥ drempel in fase 2 → hold tot $1 resolutie")
    ep2.metric("Trail activatie", f"{cfg.get('trail_activate_cts', 5)}¢",
               help="+5¢ boven entry → trailing stop activeer")
    ep3.metric("Trail buffer", f"{cfg.get('trail_buffer_cts', 2)}¢",
               help="Exit als piek − 2¢")
    st.caption("Hold drempel is zelf-lerend op basis van de laatste 7 dagen win-rate.")

    st.divider()

    # ── Recente windows ────────────────────────────────────────────────────────
    st.markdown("### Recente windows")
    rows = _q_recent_windows(coin, limit=50)
    if not rows:
        st.caption("Nog geen window data — scalper registreert elke window zodra actief.")
        return

    df = pd.DataFrame(rows)
    rename = {
        "window_start":   "Start",
        "stoplicht":      "Licht",
        "direction":      "Richting",
        "entry_price":    "Entry",
        "exit_price":     "Exit",
        "exit_reason":    "Reden",
        "trades_in_window": "#",
        "pnl_eur":        "P&L (€)",
        "paper":          "Paper",
    }
    show_cols = [c for c in rename if c in df.columns]
    show = df[show_cols].rename(columns=rename).copy()

    for col in ["Entry", "Exit"]:
        if col in show.columns:
            show[col] = show[col].apply(
                lambda x: f"{x:.3f}" if x is not None and x == x else "—"
            )
    if "P&L (€)" in show.columns:
        show["P&L (€)"] = show["P&L (€)"].apply(
            lambda x: f"€{x:+.4f}" if x is not None and x == x else "—"
        )
    if "Paper" in show.columns:
        show["Paper"] = show["Paper"].apply(lambda x: "ja" if x else "nee")
    if "Start" in show.columns:
        show["Start"] = show["Start"].apply(
            lambda x: x[:16] if isinstance(x, str) else x
        )

    try:
        styled = show.style.map(_color_pnl, subset=["P&L (€)"])
        st.dataframe(styled, hide_index=True, use_container_width=True)
    except Exception:
        st.dataframe(show, hide_index=True, use_container_width=True)
