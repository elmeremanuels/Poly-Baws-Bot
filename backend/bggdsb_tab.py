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
        be_need = float(win.get("be_needed", 0))
        be_ok   = win.get("breakeven_reached", False)
        other_side = "NO" if dom == "YES" else "YES"
        other_sp   = no_sp if other_side == "NO" else yes_sp
        other_mid  = no_mid if other_side == "NO" else yes_mid

        phase_labels = {
            "monitoring": "🔍 Monitoring",
            "flipping":   "🔄 Flipping",
            "confirmed":  "✅ Break-even",
            "done":       "✔ Done",
        }
        phase_label = phase_labels.get(phase, phase)

        c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
        c1.metric(f"**{coin_w}**", phase_label)
        c2.metric("YES mid", f"{yes_mid:.3f}")
        c3.metric("NO mid", f"{no_mid:.3f}")
        c4.metric("⏱ Resterend", f"{secs}s")
        c5.metric("YES €", f"€{yes_sp:.2f}")
        c6.metric("NO €", f"€{no_sp:.2f}")
        c7.metric("Totaal €", f"€{tot_sp:.2f}")

        if be_ok:
            st.success(f"**BREAK-EVEN BEREIKT** — bot koopt extra {other_side} voor maximale winst")
        elif phase == "flipping":
            tekort = be_need - other_sp
            st.warning(
                f"**FLIPPING → {other_side}** — nog €{tekort:.2f} nodig voor break-even "
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
                tekort = max(0, be_need - other_sp)
                other_ask = other_mid * 1.02
                st.markdown(f"""
**BOT IS AAN HET FLIPPEN — {other_side} is nu de favoriet**

Wat de bot doet: elke ~3s een tranche op {other_side} kopen

Als de bot stokt:
1. Open Polymarket → zoek actief {coin_w} window
2. Koop **{other_side}** voor **€{min(tekort+5, 20):.0f}** (ask ~{other_ask:.3f})
3. Herhaal tot "BREAK-EVEN BEREIKT" verschijnt

Huidige stand: €{other_sp:.1f} op {other_side} / €{be_need:.1f} nodig
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

    st.divider()

    # ── Controls ──────────────────────────────────────────────────────────────
    col_a, col_c, col_d = st.columns([3, 3, 1])

    with col_a:
        budget = st.slider(
            "💶 Budget per window (€)",
            min_value=20, max_value=50, step=5,
            value=int(get_state("bggdsb_window_budget") or 30),
            key="bggdsb_budget",
        )
        flip_t    = round(budget / 30 * 8, 1)
        confirm_t = round(budget / 30 * 15, 1)
        hedge_t   = round(budget * 0.05, 1)
        st.caption(
            f"Flip: **€{flip_t}**/3s · Confirm: **€{confirm_t}** (90s) · Hedge: €{hedge_t} (≤0.11)"
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

    if st.button("💾 Instellingen opslaan", key="bggdsb_save", disabled=not selected_coins):
        set_dashboard_state("bggdsb_window_budget", str(budget))
        set_dashboard_state("bggdsb_is5_signal_weight", str(is5_weight))
        set_dashboard_state("bggdsb_paper_mode", "1" if paper_mode else "0")
        set_dashboard_state("bggdsb_coins", json.dumps(selected_coins))
        mode_to_set = "bggdsb_paper" if paper_mode else "bggdsb_live"
        write_command("set_mode", {"mode": mode_to_set})
        coins_str = ", ".join(selected_coins)
        st.success(
            f"Opgeslagen — budget €{budget}/window · "
            f"coins: {coins_str} · is5 {is5_weight}% · modus {'paper' if paper_mode else 'LIVE'}"
        )
        _q_is5_live.clear()
        _q_stats.clear()

    st.divider()

    # ── Documentatie (expanders) ──────────────────────────────────────────────
    with st.expander("📐 Rekenvoorbeeld — dynamische chase (is5minfixedyet patroon)"):
        dom_price      = 0.495
        flip_eur       = 8.0
        confirm_eur_ex = 15.0
        hedge_pct      = 0.05
        dom_shares     = round(budget / dom_price, 1)
        hedge_eur_ex   = round(budget * hedge_pct, 2)

        st.markdown(f"""
**Aankopen per window · budget €{budget}**

| Moment | Actie | Kant | Bedrag |
|---|---|---|---|
| Window start | Volledige entry | Dominant | **€{budget}** (~{dom_shares} shares @ {dom_price}) |
| Als flip nodig | Bijkopen per 3s | Andere kant | **€{flip_eur}/tranche** tot break-even |
| Na break-even | Extra kopen per 6s | Winnende kant | €{flip_eur}/tranche |
| Laatste 90s (≥0.60) | Confirm buy | Winnende kant | **€{confirm_eur_ex}** |
| Verliezer ≤ 0.11 | Hedge | Verliezende kant | €{hedge_eur_ex} |

**Break-even formule:** flip-spend ÷ flip-prijs ≥ totale spend
""")
        st.caption(
            "De bot koopt de dominante kant ook opnieuw bij tijdens de confirm buy in de laatste 90s "
            "als die kant aan het winnen is (≥ 0.60)."
        )

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
