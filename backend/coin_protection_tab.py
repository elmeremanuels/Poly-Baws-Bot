"""🛡️ Coin Beveiliging tab — streak guard, per-coin disable/enable, Herzie Strategie."""
import pandas as pd
import streamlit as st

from src.config_loader import CONFIG
from src.commands import write_command
from src.db_sync import get_analytics_trades, get_daily_pnl, get_state, get_today_trade_count

COIN_EMOJI = {"BTC": "₿", "ETH": "Ξ", "SOL": "◎", "XRP": "✕", "DOGE": "Ð"}

_STATE_ICON = {
    "active":   ("🟢", "Actief",         "#34d399"),
    "watch":    ("🟡", "Watch (1 kans)",  "#fbbf24"),
    "disabled": ("🔴", "Uitgeschakeld",  "#f87171"),
}


def _guard(coin: str) -> dict:
    state = get_state(f"cg_{coin}_state") or "active"
    streak = int(get_state(f"cg_{coin}_streak") or 0)
    reason = get_state(f"cg_{coin}_reason") or ""
    return {"state": state, "streak": streak, "reason": reason}


def _overview_table() -> None:
    coins = list(CONFIG.get("coins", {}).keys())
    rows = []
    for coin in coins:
        g = _guard(coin)
        icon, label, _ = _STATE_ICON.get(g["state"], ("⚪", "?", ""))
        daily = get_daily_pnl(coin)
        count = get_today_trade_count(coin)
        rows.append({
            "": f"{COIN_EMOJI.get(coin,'')} {coin}",
            "Status": f"{icon} {label}",
            "Reeks": g["streak"],
            "Dag P&L": f"€{daily:+.2f}",
            "Trades vandaag": count,
            "Reden": g["reason"] or "—",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


def _render_herzie_result(coin: str, result: dict) -> None:
    conf = result.get("confidence", 0)
    st.markdown(f"**Vertrouwen:** {conf*100:.0f}%")

    summary = result.get("performance_summary", "")
    if summary:
        st.info(summary)

    issues = result.get("key_issues", [])
    if issues:
        st.markdown("**Knelpunten:**")
        for issue in issues:
            st.markdown(f"- {issue}")

    global_params = result.get("global_coin_params", {})
    if global_params:
        cfg_coin = CONFIG.get("coins", {}).get(coin, {})
        trading_cfg = CONFIG.get("trading", {})
        st.markdown("**Voorgestelde basisparameters voor dit coin:**")
        rows = []
        for param, val in global_params.items():
            current = cfg_coin.get(param, trading_cfg.get(param, "—"))
            rows.append({"Parameter": param, "Huidig": current, "Voorgesteld": round(val, 4) if isinstance(val, float) else val})
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    regime_specific = result.get("regime_specific", {})
    if regime_specific:
        st.markdown("**Regime-specifieke voorstellen:**")
        for regime, data in regime_specific.items():
            with st.expander(regime):
                assessment = data.get("assessment", "")
                if assessment:
                    st.caption(assessment)
                params = data.get("suggested_params", {})
                if params:
                    prows = [{"Parameter": k, "Waarde": round(v, 4) if isinstance(v, float) else v}
                             for k, v in params.items()]
                    st.dataframe(pd.DataFrame(prows), hide_index=True)

    reasoning = result.get("reasoning", "")
    if reasoning:
        with st.expander("Volledige redenering"):
            st.markdown(reasoning)

    # Conviction gate advies
    cg = result.get("conviction_gate_advice", {})
    if cg:
        min_score = cg.get("recommended_min_score", 0.0)
        cg_reasoning = cg.get("reasoning", "")
        if min_score and min_score > 0:
            st.info(f"**Conviction gate:** handel alleen bij score ≥ {min_score:.2f} — {cg_reasoning}")
        elif cg_reasoning:
            st.caption(f"Conviction gate: {cg_reasoning}")

    # Tijdstip advies
    timing = result.get("timing_advice", "")
    if timing:
        st.info(f"**Tijdstip:** {timing}")

    # YAML snippet for easy copy-paste into config.yaml
    yaml_lines = [f"  # {coin} — herziene parameters (gegenereerd door Herzie Strategie):"]
    for k, v in global_params.items():
        yaml_lines.append(f"  {k}: {v}")
    if yaml_lines:
        st.markdown("**Kopieer naar `config.yaml` → `coins.{coin}`:**")
        st.code("\n".join(yaml_lines), language="yaml")

    if st.button("🗑️ Wis analyse", key=f"clear_herzie_{coin}"):
        del st.session_state[f"herzie_{coin}"]
        st.rerun()


def coin_protection_panel() -> None:
    st.markdown("## 🛡️ Coin Beveiliging")
    st.caption(
        "De bot schakelt een coin automatisch uit na **2 opeenvolgende verliezen + 1 extra test-trade die ook verliest**, "
        "of als de dagelijkse P&L ≤ −€5. Heractiveer hieronder en laat Claude een herziene strategie voorstellen."
    )

    with st.expander("ℹ️ Hoe werkt de bescherming?", expanded=False):
        st.markdown("""
**State machine:**
- 🟢 **Actief** — normaal handelen
- 🟡 **Watch** — 2 verliezen op rij → geen nieuwe orders meer, maar lopende trades worden afgemaakt.
  Als de volgende trade *wint* → terug naar Actief. Als die ook verliest → Uitgeschakeld.
- 🔴 **Uitgeschakeld** — openstaande pending orders worden gecanceld, coin-schakelaar gaat uit.

**Dagcap:** als het dag-P&L voor een coin ≤ −€5 → automatisch Uitgeschakeld.

**Heractiveren:**
- *Direct* → zet coin terug op Actief, guard-reeks gereset.
- *Met 5 paper trades* → coin gaat aan, maar de volgende 5 trades tellen niet mee voor de reeks.
  Gebruik dit na een Herzie-Strategie analyse.
        """)

    st.subheader("Overzicht")
    _overview_table()

    st.divider()
    st.subheader("Beheer per coin")

    coins = list(CONFIG.get("coins", {}).keys())
    for coin in coins:
        g = _guard(coin)
        icon, label, _ = _STATE_ICON.get(g["state"], ("⚪", "?", ""))
        daily = get_daily_pnl(coin)

        header = (
            f"{COIN_EMOJI.get(coin,'')} **{coin}** — {icon} {label} "
            f"| Reeks: {g['streak']} | Dag: €{daily:+.2f}"
        )
        with st.expander(header, expanded=(g["state"] != "active")):
            if g["state"] == "disabled" and g["reason"]:
                st.error(f"🚫 Uitgeschakeld wegens: {g['reason']}")

                # ── Snelle diagnose ──────────────────────────────────────────
                _diag_key = f"_diag_{coin}"
                col_diag, _ = st.columns([2, 3])
                with col_diag:
                    if st.button("🔍 Wat ging er fout?", key=f"diag_{coin}",
                                 help="Claude diagnoseert de verliezen van de afgelopen 24 uur"):
                        from src.claude_analyzer import analyze_guard_trigger_sync
                        from src.db_sync import get_exit_reason_stats
                        with st.spinner("Claude analyseert (~15s)..."):
                            try:
                                # Laatste 24 uur — niet alleen vandaag
                                trades_t = get_analytics_trades(coin=coin, days=1)
                                exit_s   = get_exit_reason_stats(coin=coin, days=1)
                                dpnl     = sum(
                                    t.get("net_pnl") or 0 for t in trades_t
                                    if t.get("net_pnl") is not None
                                )
                                analysis = analyze_guard_trigger_sync(
                                    coin, g["reason"], dpnl, trades_t, exit_s
                                )
                                st.session_state[_diag_key] = analysis
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Diagnose mislukt: {exc}")

                if _diag_key in st.session_state:
                    with st.container(border=True):
                        st.markdown("**🔍 Claude-diagnose**")
                        st.markdown(st.session_state[_diag_key])
                        if st.button("✕ Wis diagnose", key=f"clr_diag_{coin}"):
                            del st.session_state[_diag_key]
                            st.rerun()
                st.markdown("")

            elif g["state"] == "watch":
                st.warning(
                    f"⚠️ Watch mode — {g['streak']} verlies op rij. "
                    "Eén kans nog: als de volgende trade wint gaat de coin terug naar Actief."
                )

            # ── Heractiveer knoppen ──────────────────────────────────────────
            col_a, col_b = st.columns(2)
            with col_a:
                if g["state"] in ("watch", "disabled"):
                    if st.button(f"✅ Heractiveer {coin} direct", key=f"cpt_en_{coin}"):
                        write_command("coin_guard_enable", {"coin": coin})
                        st.session_state.pop(f"_diag_{coin}", None)
                        st.toast(f"{coin} wordt heractiveerd...", icon="✅")
                        st.rerun()

            with col_b:
                if g["state"] == "disabled":
                    if st.button(f"📄 Heractiveer {coin} met 5 paper trades", key=f"pp_{coin}"):
                        write_command("coin_guard_paper_gate", {"coin": coin, "n": 5})
                        st.session_state.pop(f"_diag_{coin}", None)
                        st.toast(f"{coin}: re-enable + 5-paper-gate gestart", icon="📄")
                        st.rerun()

            st.markdown("---")
            st.markdown("**🧠 Herzie Strategie — Claude AI diepe analyse**")
            st.caption(
                f"Claude analyseert de recente trades van {coin}, de resultaten per regime "
                "en de huidige parameters. Het stelt concrete verbeteringen voor."
            )

            col_days, col_btn = st.columns([2, 3])
            with col_days:
                days_opts = {"30 dagen": 30, "7 dagen": 7, "Alle tijd": None}
                sel_days = st.selectbox("Periode", list(days_opts.keys()), key=f"hz_days_{coin}")
                hz_days = days_opts[sel_days]

            with col_btn:
                st.markdown("")  # vertical spacing
                if st.button(f"🧠 Analyseer {coin}", key=f"hz_btn_{coin}", type="primary"):
                    trades = get_analytics_trades(coin=coin, days=hz_days)
                    if len(trades) < 5:
                        st.warning(f"Te weinig data ({len(trades)} trades). Kies een langere periode.")
                    else:
                        with st.spinner(f"Claude analyseert {coin}... (kan ~30s duren)"):
                            try:
                                from src.claude_analyzer import analyze_coin_strategy_sync
                                # Altijd laatste 24u meegeven als zwaarste context
                                trades_24h = get_analytics_trades(coin=coin, days=1)
                                result = analyze_coin_strategy_sync(
                                    coin, trades,
                                    recent_trades_24h=trades_24h if trades_24h else None,
                                )
                                st.session_state[f"herzie_{coin}"] = result
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Analyse mislukt: {exc}")

            if f"herzie_{coin}" in st.session_state:
                st.markdown("### 📋 Analyse resultaat")
                _render_herzie_result(coin, st.session_state[f"herzie_{coin}"])
