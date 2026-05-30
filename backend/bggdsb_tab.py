"""BGGDSB Tab — Beter Goed Gejat Dan Slecht Bedacht

Strategie 1:1 gebaseerd op is5minfixedyet (data-analyse 2026-05-30):
  1. Entry prijs: dominante kant 0.40–0.65 (nooit boven 0.65)
  2. Budget split: 87.5% dominant / 12.5% hedge
  3. Totaal budget: €20–50 per window (instelbaar)
  4. Richting: conviction signal (OFI + funding rate)
  5. Exit: hold to expiry — geen vroegtijdige exit
  6. Optioneel: is5 live activiteit als extra bevestigingssignaal
"""
from __future__ import annotations

import streamlit as st
import pandas as pd

from src.db_sync import (
    get_state,
    set_dashboard_state,
    get_bggdsb_stats,
    get_bggdsb_trades,
    is5_live_status,
)
from src.commands import write_command

_IS5_ADDRESS = "0x2bc01f3ad80e31f5bf3d80775b044f0c67797871"

# ── Cached data ────────────────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def _q_is5_live() -> dict:
    return is5_live_status()

@st.cache_data(ttl=60)
def _q_stats() -> dict:
    return get_bggdsb_stats()

@st.cache_data(ttl=30)
def _q_trades(limit: int = 100) -> list[dict]:
    return get_bggdsb_trades(limit=limit)


# ── Main panel ────────────────────────────────────────────────────────────────

@st.fragment
def bggdsb_panel() -> None:
    st.subheader("🧠 BGGDSB")
    st.caption("*Beter Goed Gejat Dan Slecht Bedacht* — is5minfixedyet strategie 1:1")

    # ── is5 live indicator ─────────────────────────────────────────────────────
    status = _q_is5_live()
    live = status.get("live", False)
    n_recent = status.get("trades_recent", 0)
    last_seen = status.get("last_seen", "onbekend")

    ind_col, spacer = st.columns([3, 1])
    with ind_col:
        if live:
            st.success(
                f"🟢 **is5minfixedyet LIVE** — {n_recent} nieuwe trades gesignaleerd (laatste 15 min)",
                icon="📡",
            )
        else:
            st.info(
                f"⚫ is5minfixedyet offline — laatste activiteit: {last_seen}",
                icon="📡",
            )

    st.divider()

    # ── Controls ───────────────────────────────────────────────────────────────
    col_a, col_b, col_c = st.columns([2, 2, 2])

    with col_a:
        budget = st.slider(
            "💶 Budget per window (€)",
            min_value=20, max_value=50, step=5,
            value=int(get_state("bggdsb_window_budget") or 30),
            key="bggdsb_budget",
            help="Totaal per 5-min window: 87.5% dominant + 12.5% hedge",
        )

    with col_b:
        is5_weight = st.slider(
            "📡 is5 signaalgewicht (%)",
            min_value=0, max_value=100, step=10,
            value=int(get_state("bggdsb_is5_signal_weight") or 0),
            key="bggdsb_is5_weight",
            help=(
                "0% = puur eigen conviction. "
                "50% = conviction drempel -25% als is5 actief is. "
                "100% = conviction drempel -50% bij is5 bevestiging."
            ),
        )

    with col_c:
        paper_mode = st.toggle(
            "Paper mode",
            value=bool(int(get_state("bggdsb_paper_mode") or 1)),
            key="bggdsb_paper",
            help="Aan = veilig oefenen. Uit = live trades met echt geld.",
        )

    # Save button
    if st.button("💾 Instellingen opslaan", key="bggdsb_save"):
        set_dashboard_state("bggdsb_window_budget", str(budget))
        set_dashboard_state("bggdsb_is5_signal_weight", str(is5_weight))
        set_dashboard_state("bggdsb_paper_mode", "1" if paper_mode else "0")
        mode_to_set = "bggdsb_paper" if paper_mode else "bggdsb_live"
        write_command("set_mode", {"mode": mode_to_set})
        st.success(f"Opgeslagen — budget €{budget}, is5 gewicht {is5_weight}%, modus {'paper' if paper_mode else 'LIVE'}")
        _q_is5_live.clear()
        _q_stats.clear()

    st.divider()

    # ── Strategy rules + Performance ──────────────────────────────────────────
    rule_col, perf_col = st.columns(2)

    with rule_col:
        st.markdown("**Strategie regels (is5minfixedyet)**")
        st.markdown("""
| Regel | Waarde |
|---|---|
| Entry prijs | 0.40 – 0.65 dominant |
| Hedge prijs | < 0.35 andere kant |
| Split | 87.5% / 12.5% |
| Richting | OFI + funding rate |
| Exit | Hold to expiry (€1.00) |
| Timing | Eerste 90s van window |
| Win-rate (is5 data) | **75.4%** initieel |
| ROI (is5 data) | **+26.6%** over 3 dagen |
""")

    with perf_col:
        st.markdown("**BGGDSB performance (jouw bot)**")
        stats = _q_stats()
        if stats and stats.get("n", 0) > 0:
            c1, c2 = st.columns(2)
            c1.metric("Trades", stats["n"])
            c2.metric("Win%", f"{stats.get('win_pct', 0):.1f}%")
            c1.metric("Totaal P&L", f"€{stats.get('total_pnl', 0):+.2f}")
            c2.metric("Gem. P&L", f"€{stats.get('avg_pnl', 0):+.4f}")
            if stats.get("gem_entry_prijs"):
                st.caption(f"Gem. entry prijs: {stats['gem_entry_prijs']:.3f}")
        else:
            st.caption("Nog geen BGGDSB trades in de database.")
            st.caption("Activeer de modus en wacht op het eerste window.")

    st.divider()

    # ── Entry price gate indicator ─────────────────────────────────────────────
    st.markdown("**Actieve price gate**")
    saved_budget = int(get_state("bggdsb_window_budget") or 30)
    dominant_eur = round(saved_budget * 0.875, 2)
    hedge_eur = round(saved_budget * 0.125, 2)
    gate_col1, gate_col2, gate_col3 = st.columns(3)
    gate_col1.metric("Entry zone", "0.40 – 0.65", help="Buiten deze zone = skip")
    gate_col2.metric("Dominant", f"€{dominant_eur}", delta=f"{dominant_eur/saved_budget*100:.0f}%")
    gate_col3.metric("Hedge", f"€{hedge_eur}", delta=f"-{hedge_eur/saved_budget*100:.0f}%")

    st.divider()

    # ── Recent trades ──────────────────────────────────────────────────────────
    st.markdown("**Recente BGGDSB trades**")
    trades = _q_trades()
    if not trades:
        st.caption("Geen BGGDSB trades gevonden.")
    else:
        df = pd.DataFrame(trades)
        show = [c for c in [
            "created_at", "coin", "winner_side", "entry_yes_price", "entry_no_price",
            "yes_size", "no_size", "winner_exit_reason", "net_pnl", "status"
        ] if c in df.columns]
        df_s = df[show].copy()
        rename = {
            "created_at": "Tijd", "coin": "Coin", "winner_side": "Winnaar",
            "entry_yes_price": "YES prijs", "entry_no_price": "NO prijs",
            "yes_size": "YES shares", "no_size": "NO shares",
            "winner_exit_reason": "Exit reden", "net_pnl": "P&L", "status": "Status",
        }
        df_s.columns = [rename.get(c, c) for c in show]
        if "P&L" in df_s.columns:
            df_s["P&L"] = df_s["P&L"].apply(
                lambda x: f"€{x:+.4f}" if x is not None else ""
            )
        st.dataframe(df_s, hide_index=True, use_container_width=True)
        st.caption(f"{len(trades)} trades")
