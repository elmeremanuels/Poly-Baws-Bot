"""BGGDSB Tab — Beter Goed Gejat Dan Slecht Bedacht

Strategie 1:1 gebaseerd op is5minfixedyet (data-analyse 2026-05-30):
  1. Market competitive gate: beide kanten 0.10–0.90 (anders al bijna resolved)
  2. Richting: conviction signal (OFI + funding rate) → dominant kant
  3. Dominant kant: mediaan 8 tranches (~elke 14s), geen prijsgate na eerste entry
  4. Hedge: koop tegenovergestelde kant LAAT zodra prijs ≤ 0.15 (mediaan 0.11)
  5. Totaal budget: €20–50 per window (instelbaar)
  6. Exit: hold to expiry (€1.00) — geen vroegtijdige exit
  7. Optioneel: is5 live activiteit als extra bevestigingssignaal
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

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
_ALL_COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]

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


def _load_selected_coins() -> list[str]:
    raw = get_state("bggdsb_coins")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [c for c in parsed if c in _ALL_COINS]
        except Exception:
            pass
    return ["BTC"]


def _bot_status() -> tuple[bool, str]:
    """Geeft (online, leeftijd_tekst) op basis van heartbeat."""
    hb = get_state("heartbeat")
    if not hb:
        return False, "nooit"
    try:
        ts = datetime.fromisoformat(hb.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age < 15:
            return True, f"{int(age)}s geleden"
        if age < 60:
            return False, f"{int(age)}s geleden"
        return False, f"{int(age//60)}min geleden"
    except Exception:
        return False, "onbekend"


# ── Main panel ────────────────────────────────────────────────────────────────

@st.fragment
def bggdsb_panel() -> None:
    st.subheader("🧠 BGGDSB")
    st.caption("*Beter Goed Gejat Dan Slecht Bedacht* — is5minfixedyet strategie 1:1")

    # ── Bot + is5 status ──────────────────────────────────────────────────────
    bot_online, bot_age = _bot_status()
    status = _q_is5_live()
    live = status.get("live", False)
    n_recent = status.get("trades_recent", 0)
    last_seen = status.get("last_seen", "onbekend")

    bot_col, is5_col = st.columns(2)
    with bot_col:
        if bot_online:
            st.success(f"🟢 **Bot actief** — heartbeat {bot_age}", icon="🤖")
        else:
            st.error(f"🔴 **Bot offline** — laatste heartbeat: {bot_age}", icon="🤖")
    with is5_col:
        if live:
            st.success(
                f"🟢 **is5minfixedyet LIVE** — {n_recent} trades (laatste 15 min)",
                icon="📡",
            )
        else:
            st.info(f"⚫ is5minfixedyet offline — laatste: {last_seen}", icon="📡")

    # Huidige mode tonen
    current_mode = get_state("current_mode") or "onbekend"
    bggdsb_active = current_mode in ("bggdsb_paper", "bggdsb_live")
    if bggdsb_active:
        label = "🟡 Paper" if current_mode == "bggdsb_paper" else "💸 Live"
        st.success(f"**BGGDSB modus actief** — {label}", icon="🧠")
    else:
        st.warning(f"BGGDSB **niet actief** — huidige modus: `{current_mode}`. Sla instellingen op om te starten.", icon="⚠️")

    st.divider()

    # ── Controls rij 1: budget / tranches / is5 / paper ──────────────────────
    col_a, col_b, col_c, col_d = st.columns([2, 2, 2, 1])

    with col_a:
        budget = st.slider(
            "💶 Budget per window (€)",
            min_value=20, max_value=50, step=5,
            value=int(get_state("bggdsb_window_budget") or 30),
            key="bggdsb_budget",
            help="Totaal per 5-min window: ~89% dominant (tranches) + ~11% late hedge (bij ≤0.15)",
        )

    with col_b:
        tranches = st.slider(
            "🔁 Tranches per window",
            min_value=1, max_value=20, step=1,
            value=int(get_state("bggdsb_tranches") or 8),
            key="bggdsb_tranches",
            help=(
                "is5minfixedyet koopt mediaan 8× per window (~elke 14s), max 37+. "
                "Alleen de dominant kant. Hedge wordt apart gekocht zodra ≤0.15. "
                "Meer tranches = betere prijsgemiddeling."
            ),
        )

    with col_c:
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

    with col_d:
        paper_mode = st.toggle(
            "Paper mode",
            value=bool(int(get_state("bggdsb_paper_mode") or 1)),
            key="bggdsb_paper",
            help="Aan = veilig oefenen. Uit = live trades met echt geld.",
        )

    # ── Controls rij 2: coin selectie ────────────────────────────────────────
    st.markdown("**Coins**")
    saved_coins = _load_selected_coins()

    coin_cols = st.columns(len(_ALL_COINS))
    selected_coins = []
    for i, coin in enumerate(_ALL_COINS):
        with coin_cols[i]:
            checked = st.checkbox(
                coin,
                value=(coin in saved_coins),
                key=f"bggdsb_coin_{coin}",
                help="is5 handelde vrijwel alleen BTC" if coin == "BTC" else None,
            )
            if checked:
                selected_coins.append(coin)

    if not selected_coins:
        st.warning("Selecteer minstens één coin.", icon="⚠️")

    # ── Save button ───────────────────────────────────────────────────────────
    if st.button("💾 Instellingen opslaan", key="bggdsb_save", disabled=not selected_coins):
        set_dashboard_state("bggdsb_window_budget", str(budget))
        set_dashboard_state("bggdsb_tranches", str(tranches))
        set_dashboard_state("bggdsb_is5_signal_weight", str(is5_weight))
        set_dashboard_state("bggdsb_paper_mode", "1" if paper_mode else "0")
        set_dashboard_state("bggdsb_coins", json.dumps(selected_coins))
        mode_to_set = "bggdsb_paper" if paper_mode else "bggdsb_live"
        write_command("set_mode", {"mode": mode_to_set})
        coins_str = ", ".join(selected_coins)
        tranche_eur = round(budget / tranches, 2)
        st.success(
            f"Opgeslagen — budget €{budget} · {tranches}× €{tranche_eur}/tranche · "
            f"coins: {coins_str} · is5 {is5_weight}% · modus {'paper' if paper_mode else 'LIVE'}"
        )
        _q_is5_live.clear()
        _q_stats.clear()

    st.divider()

    # ── Rekenvoorbeeld: schaling ten opzichte van is5 ─────────────────────────
    with st.expander("📐 Rekenvoorbeeld — tranches + late hedge (is5minfixedyet patroon)"):
        saved_tranches = int(get_state("bggdsb_tranches") or 8)
        tranche_budget = round(budget / saved_tranches, 2)
        dom_price = 0.495  # mediaan eerste entry is5

        rows_md = ""
        total_dom_shares = 0.0
        for i in range(1, saved_tranches + 1):
            ds = round(tranche_budget / dom_price, 1)
            total_dom_shares += ds
            rows_md += f"| T{i} (+{(i-1)*14}s) | €{tranche_budget} | {ds} shares @ ~{dom_price:.2f} |\n"

        # Hedge: 11% van budget, gekocht LAAT bij ~0.11
        hedge_eur = round(budget * 0.11, 2)
        hedge_price = 0.11  # mediaan is5 hedge prijs
        hedge_shares = round(hedge_eur / hedge_price, 1)

        win_gross  = round(total_dom_shares * 1.0 - budget, 2)
        lose_gross = round(hedge_shares * 1.0 - budget, 2)
        ev = round(0.754 * win_gross + 0.246 * lose_gross, 2)

        st.markdown(f"""
**{saved_tranches} tranches · €{budget} totaal · €{tranche_budget}/tranche · dom entry ~{dom_price:.2f}**

| Tranche | Inleg | Dominant kant |
|---|---|---|
{rows_md}
| **Totaal dom** | **€{budget - hedge_eur:.2f}** | **{round(total_dom_shares,1)} shares** |

**Late hedge** (zodra tegengestelde kant ≤ 0.15):
€{hedge_eur} · ~{hedge_price:.2f}/share → {hedge_shares} shares

| Scenario | Payout | P&L | ROI |
|---|---|---|---|
| Dominant wint | €{round(total_dom_shares,1)} | **€{win_gross:+.2f}** | **{round(win_gross/budget*100,1):+.1f}%** |
| Dominant verliest | €{round(hedge_shares,1)} | **€{lose_gross:+.2f}** | **{round(lose_gross/budget*100,1):+.1f}%** |
| Verwachte waarde (75.4% WR) | | **€{ev:+.2f}/window** | |
""")
        st.caption(
            "Het aantal tranches schaalt NIET mee met het budget — dat is de strategie zelf. "
            "is5 koopt mediaan 8× dominant, dan LAAT een kleine hedge zodra de tegengestelde kant "
            "≤0.15 is (betekent: markt denkt bijna zeker dat dominant wint)."
        )

    st.divider()

    # ── Strategy rules + Performance ──────────────────────────────────────────
    rule_col, perf_col = st.columns(2)

    with rule_col:
        st.markdown("**Strategie regels (is5minfixedyet — exact)**")
        st.markdown("""
| Regel | Waarde |
|---|---|
| Market gate | Beide kanten 0.10 – 0.90 |
| Richting | OFI + funding rate (conviction) |
| Dominant tranches | Mediaan **8×** (~elke 14s) |
| Geen prijsgate hercheck | Koopt door ongeacht beweging |
| Hedge timing | **LAAT** — zodra andere kant ≤ 0.15 |
| Hedge budget | ~11% van window budget |
| Hedge mediaan prijs | **0.11** (mediaan is5 data) |
| Exit | Hold to expiry (€1.00) |
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

    # ── Market gate indicator ──────────────────────────────────────────────────
    st.markdown("**Actieve market gate**")
    saved_budget = int(get_state("bggdsb_window_budget") or 30)
    saved_tranches_n = int(get_state("bggdsb_tranches") or 8)
    dominant_eur = round(saved_budget * 0.89, 2)
    hedge_eur = round(saved_budget * 0.11, 2)
    gate_col1, gate_col2, gate_col3, gate_col4 = st.columns(4)
    gate_col1.metric("Market gate", "0.10 – 0.90", help="Beide kanten moeten in deze range zijn")
    gate_col2.metric("Dominant", f"€{dominant_eur}", delta=f"{saved_tranches_n}× tranches")
    gate_col3.metric("Hedge (laat)", f"€{hedge_eur}", delta="bij ≤0.15")
    gate_col4.metric("Actieve coins", ", ".join(_load_selected_coins()) or "—")

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
