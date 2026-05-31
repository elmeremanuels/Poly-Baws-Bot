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

@st.fragment(run_every=5)
def bggdsb_panel() -> None:
    import json as _json

    st.subheader("🧠 BGGDSB")
    st.caption("*Beter Goed Gejat Dan Slecht Bedacht* — is5minfixedyet strategie 1:1")

    # ── Bot + is5 status ──────────────────────────────────────────────────────
    bot_online, bot_age = _bot_status()
    status = _q_is5_live()
    live = status.get("live", False)
    n_recent = status.get("trades_recent", 0)
    last_seen = status.get("last_seen", "onbekend")

    bot_col, is5_col, mode_col = st.columns(3)
    with bot_col:
        if bot_online:
            st.success(f"🟢 **Bot** — heartbeat {bot_age}", icon="🤖")
        else:
            st.error(f"🔴 **Bot offline** — {bot_age}", icon="🤖")
    with is5_col:
        if live:
            st.success(f"🟢 **is5** LIVE — {n_recent} trades", icon="📡")
        else:
            st.info(f"⚫ **is5** offline — {last_seen}", icon="📡")
    with mode_col:
        current_mode = get_state("mode") or "onbekend"
        bggdsb_active = current_mode in ("bggdsb_paper", "bggdsb_live")
        if bggdsb_active:
            label = "🟡 Paper" if current_mode == "bggdsb_paper" else "💸 Live"
            st.success(f"**BGGDSB** — {label}", icon="🧠")
        else:
            st.warning(f"Modus: `{current_mode}`", icon="⚠️")

    st.divider()

    # ── Performance metrics ───────────────────────────────────────────────────
    stats = _q_stats()
    if stats and stats.get("n", 0) > 0:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Trades", stats["n"])
        m2.metric("Win%", f"{stats.get('win_pct', 0):.1f}%")
        m3.metric("Totaal P&L", f"€{stats.get('total_pnl', 0):+.2f}")
        m4.metric("Gem. P&L", f"€{stats.get('avg_pnl', 0):+.4f}")
    else:
        st.caption("📊 Nog geen afgeronde BGGDSB trades — metrics verschijnen na het eerste window.")

    # ── Streak status ─────────────────────────────────────────────────────────
    streak_wins = int(get_state("bggdsb_streak_wins") or 0)
    streak_skip = int(get_state("bggdsb_streak_skip") or 0)
    if streak_skip > 0:
        st.warning(
            f"⏸ **Streak onderbroken** — {streak_skip} window(s) overgeslagen "
            f"(streak herstel na verlies na {streak_wins + (streak_skip)} wins op rij)",
            icon="🛑",
        )
    elif streak_wins >= 2:
        st.success(f"🔥 **Winning streak: {streak_wins}** wins op rij", icon="🔥")
    elif streak_wins == 1:
        st.caption("✅ 1 win op rij")

    # ── Live window status ────────────────────────────────────────────────────
    raw_win = get_state("bggdsb_active_window") or ""
    try:
        win = _json.loads(raw_win) if raw_win else None
    except Exception:
        win = None

    if win and win.get("secs_left", 0) > 0:
        coin_w  = win.get("coin", "?")
        phase   = win.get("phase", "monitoring")
        dom     = win.get("dominant_side", "?")
        yes_sp  = float(win.get("yes_spend", 0))
        no_sp   = float(win.get("no_spend", 0))
        tot_sp  = yes_sp + no_sp
        yes_mid = float(win.get("yes_mid", 0))
        no_mid  = float(win.get("no_mid", 0))
        secs    = int(win.get("secs_left", 0))
        yes_sh  = float(win.get("yes_shares", 0))
        no_sh   = float(win.get("no_shares", 0))

        other_side = "NO" if dom == "YES" else "YES"
        other_sp   = no_sp  if other_side == "NO" else yes_sp
        other_mid  = no_mid if other_side == "NO" else yes_mid
        other_sh   = no_sh  if other_side == "NO" else yes_sh

        # Breakeven — computed from actual shares held
        be_ok   = tot_sp > 0 and other_sh >= tot_sp
        be_need = max(0.0, round((tot_sp - other_sh) * max(other_mid, 0.01), 2))

        # Virtual P&L — payout if current winner prices become final result
        v_winner    = "YES" if yes_mid >= no_mid else "NO"
        v_winner_sh = yes_sh if v_winner == "YES" else no_sh
        virtual_pnl = round(v_winner_sh - tot_sp, 2) if tot_sp > 0 else 0.0

        if abs(virtual_pnl) < 1.0:
            pnl_color = "#ff9800"   # orange — < €1 margin either way
        elif virtual_pnl > 0:
            pnl_color = "#00c853"   # green
        else:
            pnl_color = "#ef5350"   # red

        phase_labels = {
            "monitoring": "🔍 Monitoring",
            "flipping":   "🔄 Flipping",
            "confirmed":  "✅ Break-even",
            "done":       "✔ Done",
        }
        phase_label = phase_labels.get(phase, phase)

        # Row 1: identity + prices + time + virtual P&L
        r1c1, r1c2, r1c3, r1c4, r1c5 = st.columns([2, 1, 1, 1, 1.5])
        r1c1.metric(f"**{coin_w}**", phase_label)
        r1c2.metric("YES mid", f"{yes_mid:.3f}")
        r1c3.metric("NO mid", f"{no_mid:.3f}")
        r1c4.metric("⏱ Resterend", f"{secs}s")
        with r1c5:
            st.markdown(
                "<p style='font-size:0.85em;color:rgba(49,51,63,0.6);margin:0 0 4px 0;'>"
                "Virtuele P&amp;L</p>"
                f"<p style='font-size:1.6em;font-weight:bold;color:{pnl_color};margin:0;'>"
                f"€{virtual_pnl:+.2f}</p>",
                unsafe_allow_html=True,
            )

        # Row 2: spend + shares per side + total
        r2c1, r2c2, r2c3, r2c4, r2c5 = st.columns(5)
        r2c1.metric("YES €", f"€{yes_sp:.2f}")
        r2c2.metric("YES shares", f"{yes_sh:.2f}")
        r2c3.metric("NO €", f"€{no_sp:.2f}")
        r2c4.metric("NO shares", f"{no_sh:.2f}")
        r2c5.metric("Totaal €", f"€{tot_sp:.2f}")

        if be_ok:
            st.success(f"**BREAK-EVEN BEREIKT** — bot koopt extra {other_side} voor maximale winst")
        elif phase == "flipping":
            st.warning(
                f"**FLIPPING → {other_side}** — nog €{be_need:.2f} nodig voor break-even "
                f"(bot koopt automatisch in tranches)"
            )
        else:
            winner_mid = max(yes_mid, no_mid)
            winner = "YES" if yes_mid >= no_mid else "NO"
            if winner_mid >= 0.55:
                st.info(f"**{dom} dominant** — winnaar lijkt {winner} ({winner_mid:.2f}), bot monitort")
            else:
                st.info("**Monitoring** — markt onbeslist, bot wacht op flip-signaal (andere kant > 0.50)")

        with st.expander("📋 Handmatige instructies (als bot niet reageert)"):
            if phase == "flipping" or (other_mid > 0.50 and other_mid > (yes_mid if dom == "YES" else no_mid)):
                other_ask = other_mid * 1.02
                st.markdown(f"""
**BOT IS AAN HET FLIPPEN — {other_side} is nu de favoriet**

Wat de bot doet: elke ~3s een tranche op {other_side} kopen

Als de bot stokt:
1. Open Polymarket → zoek actief {coin_w} window
2. Koop **{other_side}** voor **€{min(be_need + 5, 20):.0f}** (ask ~{other_ask:.3f})
3. Herhaal tot "BREAK-EVEN BEREIKT" verschijnt

Huidige stand: €{other_sp:.1f} op {other_side} / €{be_need:.1f} nog nodig
                """)
            elif phase == "confirmed" or be_ok:
                winner = "YES" if yes_mid >= no_mid else "NO"
                winner_mid_v = max(yes_mid, no_mid)
                st.markdown(f"""
**BREAK-EVEN BEREIKT — winnaar is {winner} ({winner_mid_v:.2f})**

Bot koopt nog extra {winner} voor meer winst.
Confirm buy (€15) volgt automatisch in de laatste 90s als winnaar ≥ 0.60.
                """)
            else:
                st.markdown(f"""
**MONITORING — markt onbeslist**

Bot wacht tot {other_side} boven 0.50 stijgt. Jij doet: NIETS.
Prijzen: YES={yes_mid:.3f} | NO={no_mid:.3f}
                """)
    else:
        st.caption("⏳ Geen actief window — bot zoekt volgende BTC window (~elke 5 min)")

    st.divider()

    # ── Recente trades ────────────────────────────────────────────────────────
    st.markdown("**Recente trades**")
    trades = _q_trades()
    if not trades:
        st.caption("Geen BGGDSB trades gevonden.")
    else:
        df = pd.DataFrame(trades)
        n_aborted = int((df["status"] == "aborted").sum()) if "status" in df.columns else 0
        df = df[df["status"] != "aborted"].copy() if "status" in df.columns else df

        # Afleiding: instaprichting uit yes_size/no_size
        def _dir(row) -> str:
            ys = row.get("yes_size") or 0
            return "Up" if float(ys) > 0 else "Down"

        def _correct(row) -> str:
            winner = row.get("winner_side")
            if not winner:
                return ""
            entry_is_yes = float(row.get("yes_size") or 0) > 0
            won = (entry_is_yes and winner == "YES") or (not entry_is_yes and winner == "NO")
            return "✅" if won else "❌"

        df["Richting"]  = df.apply(_dir, axis=1)
        df["Juist"]     = df.apply(_correct, axis=1)

        # YES/NO → Up/Down voor Winnaar
        df["winner_side"] = df["winner_side"].map({"YES": "Up", "NO": "Down"}).fillna("")

        # Instap prijs = whichever side was entered
        df["entry_price"] = df.apply(
            lambda r: r.get("entry_yes_price") if float(r.get("yes_size") or 0) > 0
                      else r.get("entry_no_price"),
            axis=1,
        )
        # Aandelen = dom side shares
        df["entry_shares"] = df.apply(
            lambda r: r.get("yes_size") if float(r.get("yes_size") or 0) > 0
                      else r.get("no_size"),
            axis=1,
        )

        cols = [c for c in [
            "created_at", "coin", "Richting", "entry_price", "entry_shares",
            "winner_side", "Juist", "winner_exit_reason", "net_pnl", "status"
        ] if c in df.columns]
        df_s = df[cols].copy()
        df_s.columns = [
            {"created_at": "Tijd", "coin": "Coin", "entry_price": "Instap prijs",
             "entry_shares": "Aandelen", "winner_side": "Winnaar",
             "winner_exit_reason": "Exit reden", "net_pnl": "P&L",
             "status": "Status"}.get(c, c)
            for c in cols
        ]
        if "Instap prijs" in df_s.columns:
            df_s["Instap prijs"] = df_s["Instap prijs"].apply(
                lambda x: f"{float(x):.3f}" if pd.notna(x) and x is not None else ""
            )
        if "Aandelen" in df_s.columns:
            df_s["Aandelen"] = df_s["Aandelen"].apply(
                lambda x: f"{float(x):.2f}" if pd.notna(x) and x is not None else ""
            )
        if "P&L" in df_s.columns:
            df_s["P&L"] = df_s["P&L"].apply(
                lambda x: f"€{float(x):+.2f}" if pd.notna(x) and x is not None else ""
            )
        st.dataframe(df_s, hide_index=True, use_container_width=True)
        n_correct = int((df["Juist"] == "✅").sum())
        n_decided = int((df["Juist"].isin(["✅", "❌"])).sum())
        caption_parts = [f"{len(df_s)} trades"]
        if n_decided:
            caption_parts.append(f"richting {n_correct}/{n_decided} juist ({n_correct/n_decided*100:.0f}%)")
        if n_aborted:
            caption_parts.append(f"{n_aborted} instap-pogingen verborgen")
        st.caption(" · ".join(caption_parts))

    st.divider()

    # ── Controls ──────────────────────────────────────────────────────────────
    if not bggdsb_active:
        st.info(
            f"Instellingen zijn uitgeschakeld — bot staat in `{current_mode}` modus. "
            "Selecteer **🧠 BGGDSB paper** of **🔴 BGGDSB live** in de sidebar om te bewerken.",
            icon="🔒",
        )
    col_a, col_c, col_d = st.columns([3, 3, 1])

    with col_a:
        _saved_budget = int(get_state("bggdsb_window_budget") or 2)
        budget = st.slider(
            "💶 Budget per window (€)",
            min_value=2, max_value=200, step=1,
            value=_saved_budget,
            key="bggdsb_budget",
            disabled=not bggdsb_active,
            help=None if bggdsb_active else "Activeer eerst BGGDSB modus via de sidebar.",
        )
        avg_down_t   = round(budget * 0.50, 2)
        flip_t       = round(budget * 0.75, 2)
        confirm_t    = round(budget * 1.00, 2)
        hedge_t      = round(budget * 0.10, 2)
        max_possible = round(budget + budget * 4.00 + budget * 3.00 + budget * 1.00 + budget * 0.10, 2)
        st.caption(
            f"Avg-down: **€{avg_down_t}**/tranche · Flip: **€{flip_t}**/tranche · "
            f"Confirm: **€{confirm_t}** (90s) · Hedge: **€{hedge_t}** (≤0.11) · "
            f"⚠️ Max totaal zonder limiet: **€{max_possible}**"
        )

    with col_c:
        is5_weight = st.slider(
            "📡 is5 signaalgewicht (%)",
            min_value=0, max_value=100, step=10,
            value=int(get_state("bggdsb_is5_signal_weight") or 0),
            key="bggdsb_is5_weight",
            disabled=not bggdsb_active,
            help=(
                "Activeer eerst BGGDSB modus via de sidebar." if not bggdsb_active else
                "0% = is5 nooit gebruikt (puur marktprijs). "
                "6% ≈ origineel (tiebreaker bij prijsverschil ≤ 3ct). "
                "50% = is5 tiebreaker tot 25ct verschil. "
                "100% = is5 altijd boven marktprijs."
            ),
        )

    with col_d:
        st.markdown("&nbsp;", unsafe_allow_html=True)  # vertical align

    # Coins row
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
                disabled=not bggdsb_active,
                help="is5 handelde vrijwel alleen BTC" if coin == "BTC" else None,
            )
            if checked:
                selected_coins.append(coin)

    if not selected_coins:
        st.warning("Selecteer minstens één coin.", icon="⚠️")

    # Current live status
    _is_live_now = get_state("bggdsb_paper_mode") == "0"

    btn_col, live_col = st.columns([2, 3])
    with btn_col:
        if st.button("💾 Instellingen opslaan (paper)", key="bggdsb_save",
                     disabled=not selected_coins or not bggdsb_active):
            set_dashboard_state("bggdsb_window_budget", str(budget))
            set_dashboard_state("bggdsb_is5_signal_weight", str(is5_weight))
            set_dashboard_state("bggdsb_paper_mode", "1")   # altijd paper na opslaan
            set_dashboard_state("bggdsb_coins", json.dumps(selected_coins))
            write_command("set_mode", {"mode": "bggdsb_paper"})
            coins_str = ", ".join(selected_coins)
            st.success(
                f"Opgeslagen in **paper** mode — budget €{budget}/window · coins: {coins_str}"
            )
            _q_is5_live.clear()
            _q_stats.clear()

    with live_col:
        if _is_live_now:
            if st.button("⏸ Terug naar paper", key="bggdsb_to_paper", type="secondary"):
                set_dashboard_state("bggdsb_paper_mode", "1")
                write_command("set_mode", {"mode": "bggdsb_paper"})
                st.warning("Teruggeschakeld naar **paper** mode.")
        else:
            st.warning(
                f"⚠️ LIVE modus gebruikt **echt geld** — €{budget} per window. "
                "Zeker weten?",
                icon="🔴",
            )
            if st.button(
                f"🔴 Ga LIVE (€{budget}/window, echt geld!)",
                key="bggdsb_go_live",
                type="primary",
                disabled=not selected_coins,
            ):
                set_dashboard_state("bggdsb_window_budget", str(budget))
                set_dashboard_state("bggdsb_is5_signal_weight", str(is5_weight))
                set_dashboard_state("bggdsb_paper_mode", "0")
                set_dashboard_state("bggdsb_coins", json.dumps(selected_coins))
                write_command("set_mode", {"mode": "bggdsb_live"})
                st.error(
                    f"🔴 LIVE actief — €{budget}/window op {', '.join(selected_coins)}. "
                    "Klik 'Terug naar paper' om te stoppen."
                )

    st.divider()

    # ── Documentatie (expanders) ──────────────────────────────────────────────
    with st.expander("📐 Rekenvoorbeeld — is5minfixedyet strategie"):
        dom_price      = 0.50
        dom_shares     = round(budget / dom_price, 1)
        avg_down_ex    = round(budget * 0.50, 2)
        avg_down_max   = round(budget * 4.00, 2)
        flip_tranche_ex = round(budget * 0.75, 2)
        flip_max_ex    = round(budget * 3.00, 2)
        confirm_ex     = round(budget * 1.00, 2)
        hedge_eur_ex   = round(budget * 0.10, 2)

        st.markdown(f"""
**Aankopen per window · budget €{budget}**

| Moment | Actie | Kant | Bedrag |
|---|---|---|---|
| Window start | Volledige entry | Dominant | **€{budget}** (~{dom_shares} shares @ {dom_price}) |
| Prijs daalt > 8ct | Averaging down | Dominant (bijkopen) | **€{avg_down_ex}/tranche** (max €{avg_down_max} totaal) |
| Andere kant > 0.62 | Flip | Andere kant | **€{flip_tranche_ex}/tranche** elke 20s (max €{flip_max_ex}) |
| Laatste 90s (≥0.78) | Confirm buy | Winnende kant | **€{confirm_ex}** eenmalig |
| Verliezer ≤ 0.11 | Hedge | Verliezende kant | **€{hedge_eur_ex}** |

*Exit: hold to expiry (€1.00 per winnende share)*
""")

    with st.expander("📋 Strategie regels + Market gate"):
        rule_col, gate_col = st.columns(2)
        with rule_col:
            st.markdown("""
| Regel | Waarde |
|---|---|
| Market gate | Beide kanten 0.10 – 0.90 |
| Richting | OFI + funding rate (conviction) |
| Initiële entry | Volledig budget op dominante kant |
| Flip trigger | Andere kant > 0.50 én wint |
| Flip tranches | €8/tranche elke 3s tot break-even |
| Confirm buy | €15 op winnaar in laatste 90s (≥ 0.60) |
| Hedge timing | Verliezer ≤ 0.11 → 5% budget |
| Exit | Hold to expiry (€1.00) |
| Win-rate (is5 data) | **75.4%** initieel |
| ROI (is5 data) | **+26.6%** over 3 dagen |
""")
        with gate_col:
            saved_budget = int(get_state("bggdsb_window_budget") or 30)
            dominant_eur = round(saved_budget * 0.89, 2)
            hedge_eur    = round(saved_budget * 0.11, 2)
            st.metric("Market gate", "0.10 – 0.90")
            st.metric("Dominant entry", f"€{dominant_eur}")
            st.metric("Hedge (bij ≤0.11)", f"€{hedge_eur}")
            st.metric("Actieve coins", ", ".join(_load_selected_coins()) or "—")
