"""🛡️ Coin Beveiliging tab — streak guard, per-coin disable/enable, Herzie Strategie."""
from pathlib import Path

import pandas as pd
import streamlit as st

from src.config_loader import CONFIG
from src.commands import write_command
from src.db_sync import get_analytics_trades, get_daily_pnl, get_state, get_today_trade_count

COIN_EMOJI = {"BTC": "₿", "ETH": "Ξ", "SOL": "◎", "XRP": "✕", "DOGE": "Ð"}

_STATE_ICON = {
    "active":     ("🟢", "Actief",           "#34d399"),
    "watch":      ("🟡", "Watch (1 kans)",   "#fbbf24"),
    "paper_only": ("📄", "Paper-only herstel","#60a5fa"),
    "disabled":   ("🔴", "Uitgeschakeld",    "#f87171"),
}

_CONFIG_PATH = Path(__file__).parent / "config" / "config.yaml"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _guard(coin: str) -> dict:
    state = get_state(f"cg_{coin}_state") or "active"
    streak = int(get_state(f"cg_{coin}_streak") or 0)
    reason = get_state(f"cg_{coin}_reason") or ""
    paper_wins = int(get_state(f"cg_{coin}_paper_wins") or 0)
    return {"state": state, "streak": streak, "reason": reason, "paper_wins": paper_wins}


def _overview_table() -> None:
    coins = list(CONFIG.get("coins", {}).keys())
    rows = []
    for coin in coins:
        g = _guard(coin)
        icon, label, _ = _STATE_ICON.get(g["state"], ("⚪", "?", ""))
        daily = get_daily_pnl(coin)
        count = get_today_trade_count(coin)
        needed = CONFIG.get("coin_guard", {}).get("paper_only_recovery_wins", 3)
        paper_prog = f"{g['paper_wins']}/{needed}" if g["state"] == "paper_only" else "—"
        rows.append({
            "": f"{COIN_EMOJI.get(coin,'')} {coin}",
            "Status": f"{icon} {label}",
            "Reeks": g["streak"],
            "Paper-wins": paper_prog,
            "Dag P&L": f"€{daily:+.2f}",
            "Trades vandaag": count,
            "Reden": g["reason"] or "—",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


def _apply_coin_params_to_config(
    coin: str,
    global_params: dict,
    regime_params: dict | None = None,
) -> str:
    """Write params to config.yaml preserving all comments.

    global_params  → config.yaml → coins.{coin}
    regime_params  → config.yaml → regime_profiles.{REGIME}  (per-regime suggestions)

    Returns error string on failure, '' on success.
    """
    try:
        from ruamel.yaml import YAML
        ryaml = YAML()
        ryaml.preserve_quotes = True
        ryaml.width = 4096

        with open(_CONFIG_PATH) as f:
            cfg = ryaml.load(f)

        # ── Coin-level params ────────────────────────────────────────────────
        if "coins" not in cfg:
            cfg["coins"] = {}
        if coin not in cfg["coins"] or cfg["coins"][coin] is None:
            cfg["coins"][coin] = {}
        for k, v in global_params.items():
            cfg["coins"][coin][k] = v

        # ── Regime-level params ──────────────────────────────────────────────
        if regime_params:
            if "regime_profiles" not in cfg:
                cfg["regime_profiles"] = {}
            for regime, rp in regime_params.items():
                if not rp:
                    continue
                if regime not in cfg["regime_profiles"] or cfg["regime_profiles"][regime] is None:
                    cfg["regime_profiles"][regime] = {}
                for k, v in rp.items():
                    cfg["regime_profiles"][regime][k] = v  # None → null in YAML

        with open(_CONFIG_PATH, "w") as f:
            ryaml.dump(cfg, f)

    except ImportError:
        # Fallback: PyYAML (loses inline comments, keeps structure)
        import yaml
        try:
            with open(_CONFIG_PATH) as f:
                cfg = yaml.safe_load(f)
            cfg.setdefault("coins", {}).setdefault(coin, {}).update(global_params)
            if regime_params:
                rp_cfg = cfg.setdefault("regime_profiles", {})
                for regime, rp in regime_params.items():
                    if rp:
                        rp_cfg.setdefault(regime, {}).update(rp)
            with open(_CONFIG_PATH, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        except Exception as exc2:
            return str(exc2)

    except Exception as exc:
        return str(exc)

    # Mirror into in-memory CONFIG
    CONFIG.setdefault("coins", {}).setdefault(coin, {}).update(global_params)
    if regime_params:
        for regime, rp in regime_params.items():
            if rp:
                CONFIG.setdefault("regime_profiles", {}).setdefault(regime, {}).update(
                    {k: v for k, v in rp.items() if v is not None}  # skip None for in-memory
                )
    return ""


# ── Render helpers ─────────────────────────────────────────────────────────────

def _render_herzie_result(coin: str, result: dict) -> None:
    """Render the full Herzie Strategie analysis result, including Pas toe / Sla over."""
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
            rows.append({
                "Parameter": param,
                "Huidig": current,
                "Voorgesteld": round(val, 4) if isinstance(val, float) else val,
            })
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

    # YAML snippet for reference
    if global_params:
        yaml_lines = [f"  # {coin} — herziene parameters (gegenereerd door Herzie Strategie):"]
        for k, v in global_params.items():
            yaml_lines.append(f"  {k}: {v}")
        st.markdown(f"**Kopieer naar `config.yaml` → `coins.{coin}`:**")
        st.code("\n".join(yaml_lines), language="yaml")

    # ── Pas toe / Sla over ────────────────────────────────────────────────────
    _applied_key = f"herzie_{coin}_applied"
    _skipped_key = f"herzie_{coin}_skipped"

    if global_params:
        if not st.session_state.get(_applied_key) and not st.session_state.get(_skipped_key):
            st.markdown("**Toepassen:**")
            _btn_a, _btn_b, _ = st.columns([2, 2, 3])
            with _btn_a:
                if st.button(
                    "✅ Pas toe in config.yaml",
                    key=f"apply_herzie_{coin}",
                    type="primary",
                    help="Schrijft basisparameters (coins.{coin}) én regime-specifieke params (regime_profiles) naar config.yaml.",
                ):
                    # Build regime_params from regime_specific (all regimes that have suggestions)
                    _regime_params: dict = {}
                    for _rgm, _rdata in regime_specific.items():
                        _rp = _rdata.get("suggested_params", {})
                        if _rp:
                            _regime_params[_rgm] = _rp

                    err = _apply_coin_params_to_config(coin, global_params, _regime_params or None)
                    if err:
                        st.error(f"Fout bij schrijven config.yaml: {err}")
                    else:
                        write_command("apply_coin_params", {
                            "coin": coin,
                            "params": global_params,
                            "regime_params": _regime_params,
                        })
                        n_regimes = len(_regime_params)
                        st.session_state[_applied_key] = True
                        st.toast(
                            f"✅ {coin}-params + {n_regimes} regime-profielen opgeslagen! "
                            "Herstart de bot voor volledige persistentie.",
                            icon="✅",
                        )
                        st.rerun()
            with _btn_b:
                if st.button("⏭ Sla over", key=f"skip_herzie_{coin}",
                             help="Bewaar de analyse maar pas de parameters niet toe."):
                    st.session_state[_skipped_key] = True
                    st.rerun()

        elif st.session_state.get(_applied_key):
            st.success(
                "✅ Coin-params én regime-profielen toegepast in `config.yaml` en naar de bot verstuurd. "
                "De bot gebruikt ze direct — herstart voor 100% persistentie."
            )
        else:
            st.info("ℹ️ Overgeslagen — parameters niet toegepast.")

    # Clear button (also resets applied/skipped state)
    if st.button("🗑️ Wis analyse", key=f"clear_herzie_{coin}"):
        for _k in (f"herzie_{coin}", f"herzie_{coin}_applied", f"herzie_{coin}_skipped"):
            st.session_state.pop(_k, None)
        st.rerun()


def _herzie_section(coin: str) -> None:
    """Render the full Herzie Strategie section: header, period selector, analyze button, result."""
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
                with st.status(
                    f"🧠 Claude analyseert {coin}…",
                    expanded=True,
                ) as _hz_status:
                    try:
                        _hz_status.write("📊 Handelsdata + signalen verzamelen…")
                        trades_24h = get_analytics_trades(coin=coin, days=1)
                        _hz_status.write(
                            "🤖 Claude API aanvragen — dit duurt ~20–40 seconden. "
                            "Als de browser een melding geeft, wacht dan nog even of ververs de pagina."
                        )
                        from src.claude_analyzer import analyze_coin_strategy_sync
                        result = analyze_coin_strategy_sync(
                            coin, trades,
                            recent_trades_24h=trades_24h if trades_24h else None,
                        )
                        _hz_status.update(
                            label=f"✅ Analyse van {coin} klaar",
                            state="complete",
                            expanded=False,
                        )
                        st.session_state[f"herzie_{coin}"] = result
                        st.rerun()
                    except Exception as exc:
                        _hz_status.update(
                            label=f"❌ Analyse mislukt — {exc}",
                            state="error",
                            expanded=True,
                        )

    if f"herzie_{coin}" in st.session_state:
        st.markdown("### 📋 Analyse resultaat")
        _render_herzie_result(coin, st.session_state[f"herzie_{coin}"])


# ── Main panel ─────────────────────────────────────────────────────────────────

@st.fragment
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

            # ══════════════════════════════════════════════════════════════════
            # 🔴 DISABLED — wizard flow: diagnose → herzie → pas toe → heractiveer
            # ══════════════════════════════════════════════════════════════════
            if g["state"] == "disabled":
                if g["reason"]:
                    st.error(f"🚫 Uitgeschakeld wegens: {g['reason']}")

                # ── 1. Snelle diagnose ─────────────────────────────────────
                _diag_key = f"_diag_{coin}"
                col_diag, _ = st.columns([2, 3])
                with col_diag:
                    if st.button(
                        "🔍 Wat ging er fout?",
                        key=f"diag_{coin}",
                        help="Claude diagnoseert de verliezen van de afgelopen 24 uur",
                    ):
                        from src.claude_analyzer import analyze_guard_trigger_sync
                        from src.db_sync import get_exit_reason_stats
                        with st.status("🔍 Claude diagnoseert…", expanded=True) as _diag_status:
                            try:
                                _diag_status.write("📊 Handelsdata ophalen (24h)…")
                                trades_t = get_analytics_trades(coin=coin, days=1)
                                exit_s   = get_exit_reason_stats(coin=coin, days=1)
                                dpnl     = sum(
                                    t.get("net_pnl") or 0 for t in trades_t
                                    if t.get("net_pnl") is not None
                                )
                                _diag_status.write("🤖 Claude API — duurt ~15 seconden…")
                                analysis = analyze_guard_trigger_sync(
                                    coin, g["reason"], dpnl, trades_t, exit_s
                                )
                                _diag_status.update(
                                    label="✅ Diagnose klaar",
                                    state="complete",
                                    expanded=False,
                                )
                                st.session_state[_diag_key] = analysis
                                st.rerun()
                            except Exception as exc:
                                _diag_status.update(
                                    label=f"❌ Diagnose mislukt — {exc}",
                                    state="error",
                                    expanded=True,
                                )

                if _diag_key in st.session_state:
                    with st.container(border=True):
                        st.markdown("**🔍 Claude-diagnose**")
                        st.markdown(st.session_state[_diag_key])
                        if st.button("✕ Wis diagnose", key=f"clr_diag_{coin}"):
                            del st.session_state[_diag_key]
                            st.rerun()

                st.markdown("---")

                # ── 2. Herzie Strategie ────────────────────────────────────
                _herzie_section(coin)

                st.markdown("---")

                # ── 3. Heractiveer (bottom of wizard) ─────────────────────
                st.markdown("**Heractiveer na analyse:**")
                col_a, col_b = st.columns(2)
                with col_a:
                    if st.button(f"✅ Heractiveer {coin} direct", key=f"cpt_en_{coin}"):
                        write_command("coin_guard_enable", {"coin": coin})
                        st.session_state.pop(f"_diag_{coin}", None)
                        st.toast(f"{coin} wordt heractiveerd…", icon="✅")
                        st.rerun()
                with col_b:
                    if st.button(f"📄 Heractiveer {coin} met 5 paper trades", key=f"pp_{coin}"):
                        write_command("coin_guard_paper_gate", {"coin": coin, "n": 5})
                        st.session_state.pop(f"_diag_{coin}", None)
                        st.toast(f"{coin}: re-enable + 5-paper-gate gestart", icon="📄")
                        st.rerun()

            # ══════════════════════════════════════════════════════════════════
            # 🟡 WATCH — heractiveer first (time-sensitive), Herzie below
            # ══════════════════════════════════════════════════════════════════
            elif g["state"] == "watch":
                st.warning(
                    f"⚠️ Watch mode — {g['streak']} verlies op rij. "
                    "Eén kans nog: als de volgende trade wint gaat de coin terug naar Actief."
                )

                col_a, _ = st.columns([2, 3])
                with col_a:
                    if st.button(f"✅ Heractiveer {coin} direct", key=f"cpt_en_{coin}"):
                        write_command("coin_guard_enable", {"coin": coin})
                        st.toast(f"{coin} wordt heractiveerd…", icon="✅")
                        st.rerun()

                st.markdown("---")
                _herzie_section(coin)

            # ══════════════════════════════════════════════════════════════════
            # 📄 PAPER_ONLY — auto-herstel loopt, toon voortgang
            # ══════════════════════════════════════════════════════════════════
            elif g["state"] == "paper_only":
                needed = CONFIG.get("coin_guard", {}).get("paper_only_recovery_wins", 3)
                wins   = g["paper_wins"]
                if g["reason"]:
                    st.info(f"ℹ️ {g['reason']}")
                st.markdown(
                    f"**📄 Paper-only modus actief** — "
                    f"bot handelt `{coin}` alleen op papier totdat {needed} opeenvolgende paper-wins zijn behaald."
                )
                st.progress(min(1.0, wins / max(1, needed)),
                            text=f"{wins} / {needed} paper-wins behaald")
                col_a, col_b = st.columns(2)
                with col_a:
                    if st.button(f"✅ Forceer heractivering {coin}", key=f"cpt_en_{coin}"):
                        write_command("coin_guard_enable", {"coin": coin})
                        st.toast(f"{coin} wordt geforceerd heractiveerd…", icon="✅")
                        st.rerun()
                with col_b:
                    st.caption("Of wacht op automatisch herstel.")

                st.markdown("---")
                _herzie_section(coin)

            # ══════════════════════════════════════════════════════════════════
            # 🟢 ACTIVE — optional preventive Herzie only
            # ══════════════════════════════════════════════════════════════════
            else:
                _herzie_section(coin)
