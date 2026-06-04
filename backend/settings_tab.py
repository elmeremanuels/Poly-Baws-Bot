"""Instellingen dashboard — centraal beheer van alle config.yaml instellingen."""
from __future__ import annotations

from pathlib import Path

import streamlit as st

_CONFIG_PATH = Path(__file__).parent / "config" / "config.yaml"

COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_yaml() -> dict:
    try:
        import yaml
        with open(_CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _save_sections(sections: dict) -> str:
    """Schrijf een of meer secties naar config.yaml (bewaart commentaar via ruamel)."""
    try:
        from ruamel.yaml import YAML
        ry = YAML()
        ry.preserve_quotes = True
        ry.width = 4096
        with open(_CONFIG_PATH) as f:
            cfg = ry.load(f)
        for section, params in sections.items():
            if section not in cfg or cfg[section] is None:
                cfg[section] = {}
            if isinstance(params, dict):
                for k, v in params.items():
                    cfg[section][k] = v
            else:
                cfg[section] = params
        with open(_CONFIG_PATH, "w") as f:
            ry.dump(cfg, f)
        return ""
    except ImportError:
        pass
    except Exception as exc:
        return str(exc)
    # fallback PyYAML
    try:
        import yaml
        with open(_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        for section, params in sections.items():
            if isinstance(params, dict):
                cfg.setdefault(section, {}).update(params)
            else:
                cfg[section] = params
        with open(_CONFIG_PATH, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return ""
    except Exception as exc2:
        return str(exc2)


def _save_nested(section: str, subsection: str, params: dict) -> str:
    """Schrijf één geneste sectie (bijv. coins.BTC of signal_trader.coins.BTC)."""
    try:
        from ruamel.yaml import YAML
        ry = YAML()
        ry.preserve_quotes = True
        ry.width = 4096
        with open(_CONFIG_PATH) as f:
            cfg = ry.load(f)
        if section not in cfg or cfg[section] is None:
            cfg[section] = {}
        if subsection not in cfg[section] or cfg[section][subsection] is None:
            cfg[section][subsection] = {}
        for k, v in params.items():
            cfg[section][subsection][k] = v
        with open(_CONFIG_PATH, "w") as f:
            ry.dump(cfg, f)
        return ""
    except Exception as exc:
        return str(exc)


def _ok(msg: str = "Opgeslagen.") -> None:
    st.success(msg, icon="✅")


def _err(e: str) -> None:
    st.error(f"Opslaan mislukt: {e}", icon="🚨")


def _restart_hint() -> None:
    st.caption("De bot herlaadt de meeste instellingen via commando's. "
               "Voor structurele wijzigingen: herstart met `systemctl restart poly-baws-bot`.")


# ── Sectie-renderers ───────────────────────────────────────────────────────────

def _section_mode_coins(cfg: dict) -> None:
    st.subheader("Bot mode & coins")
    from src.config_loader import CONFIG
    try:
        from src.db_sync import get_state, set_dashboard_state
        current_mode = get_state("mode") or CONFIG.get("_mode", "paper_hybrid")
    except Exception:
        current_mode = "paper_hybrid"

    MODES = [
        "paper_hybrid", "paper_auto", "live_hybrid", "live_auto",
        "live_learning", "signal_trader", "auto_router",
        "bggdsb_paper", "bggdsb_live", "stoplicht_scalper",
    ]
    MODE_LABELS = {
        "paper_hybrid": "paper_hybrid",
        "paper_auto": "paper_auto",
        "live_hybrid": "🔴 live_hybrid",
        "live_auto": "🔴 live_auto",
        "live_learning": "🔴 live_learning",
        "signal_trader": "signal_trader",
        "auto_router": "auto_router",
        "bggdsb_paper": "🧠 BGGDSB paper",
        "bggdsb_live": "🔴 BGGDSB live",
        "stoplicht_scalper": "🚦 Stoplicht Scalper",
    }

    idx = MODES.index(current_mode) if current_mode in MODES else 0
    new_mode = st.selectbox("Actieve modus", MODES, index=idx,
                            format_func=lambda m: MODE_LABELS.get(m, m),
                            key="set_mode_sel")
    if new_mode != current_mode:
        if new_mode in ("live_hybrid", "live_auto", "live_learning", "bggdsb_live"):
            st.warning(f"**Live modus {new_mode}** verhandelt echt geld. Bevestig hieronder.")
            if st.button("✅ Bevestig: schakel naar live modus", key="confirm_live_mode"):
                try:
                    from src.commands import write_command
                    write_command("set_mode", {"mode": new_mode})
                    _ok(f"Modus ingesteld op {new_mode}.")
                    st.rerun()
                except Exception as e:
                    _err(str(e))
        else:
            try:
                from src.commands import write_command
                write_command("set_mode", {"mode": new_mode})
                _ok(f"Modus ingesteld op {new_mode}.")
                st.rerun()
            except Exception as e:
                _err(str(e))

    st.divider()
    st.markdown("**Coins**")
    coins_cfg = cfg.get("coins", {})
    changed_coins: dict[str, dict] = {}
    cols = st.columns(len(COINS))
    for i, coin in enumerate(COINS):
        c = coins_cfg.get(coin, {})
        with cols[i]:
            en = st.toggle(coin, value=bool(c.get("enabled", True)), key=f"coin_en_{coin}")
            mp = st.number_input("Max pos.", min_value=1, max_value=20,
                                 value=int(c.get("max_parallel_positions", 5)),
                                 key=f"coin_mp_{coin}", step=1, label_visibility="collapsed")
            st.caption(f"max {mp} pos.")
            if en != c.get("enabled", True) or mp != c.get("max_parallel_positions", 5):
                changed_coins[coin] = {"enabled": en, "max_parallel_positions": mp}

    if changed_coins and st.button("Coins opslaan", key="save_coins"):
        try:
            from ruamel.yaml import YAML
            ry = YAML()
            ry.preserve_quotes = True
            ry.width = 4096
            with open(_CONFIG_PATH) as f:
                full = ry.load(f)
            for coin, params in changed_coins.items():
                for k, v in params.items():
                    full["coins"][coin][k] = v
                try:
                    from src.commands import write_command
                    write_command("set_coin_config", {"coin": coin, **params})
                except Exception:
                    pass
            with open(_CONFIG_PATH, "w") as f:
                ry.dump(full, f)
            _ok("Coin-instellingen opgeslagen.")
        except Exception as e:
            _err(str(e))


def _section_risk(cfg: dict) -> None:
    st.subheader("Risk & bescherming")
    r = cfg.get("risk", {})

    col1, col2 = st.columns(2)
    with col1:
        daily = st.number_input("Dagelijks verlies limiet (€)", min_value=0.0, max_value=500.0,
                                value=float(r.get("daily_loss_limit_eur", 10.0)),
                                step=1.0, key="risk_daily")
        min_cap = st.number_input("Minimaal kapitaal floor (€)", min_value=0.0, max_value=500.0,
                                  value=float(r.get("min_capital_eur", 15.0)),
                                  step=1.0, key="risk_mincap")
        consec = st.number_input("Max verliezen op rij (0 = uit)", min_value=0, max_value=50,
                                 value=int(r.get("max_consecutive_losses", 7)),
                                 step=1, key="risk_consec")
    with col2:
        peak_dd = st.number_input("Max drawdown van piek (%)", min_value=0, max_value=100,
                                  value=int(r.get("max_drawdown_from_peak_pct", 25)),
                                  step=1, key="risk_peakdd")
        start_dd = st.number_input("Max drawdown van start (%)", min_value=0, max_value=100,
                                   value=int(r.get("max_drawdown_from_start_pct", 40)),
                                   step=1, key="risk_startdd")
        coin_daily = st.number_input("Dagelijks verlies per coin (€)", min_value=0.0, max_value=500.0,
                                     value=float(r.get("daily_coin_loss_limit_eur", 25.0)),
                                     step=1.0, key="risk_coindaily")

    if st.button("Risk opslaan", key="save_risk"):
        err = _save_sections({"risk": {
            "daily_loss_limit_eur": daily,
            "daily_coin_loss_limit_eur": coin_daily,
            "min_capital_eur": min_cap,
            "max_consecutive_losses": consec,
            "max_drawdown_from_peak_pct": peak_dd,
            "max_drawdown_from_start_pct": start_dd,
        }})
        if err:
            _err(err)
        else:
            _ok()
        _restart_hint()


def _section_trading(cfg: dict) -> None:
    st.subheader("Straddle trading")
    t = cfg.get("trading", {})
    e = cfg.get("entry", {})

    col1, col2 = st.columns(2)
    with col1:
        size = st.number_input("Trade grootte (€ per kant)", min_value=0.01, max_value=100.0,
                               value=float(t.get("trade_size_eur", 1.0)),
                               step=0.5, key="tr_size")
        trigger = st.slider("Trigger drempel", min_value=0.60, max_value=0.95,
                            value=float(t.get("trigger_threshold", 0.73)),
                            step=0.01, key="tr_trigger", format="%.2f")
        entry_start = st.number_input("Entry start (min voor window)", min_value=1, max_value=120,
                                      value=int(t.get("entry_start_minutes_before_window", 45)),
                                      step=1, key="tr_entry_start")
    with col2:
        max_scalein = st.number_input("Max scale-in budget (€, 0 = uit)", min_value=0.0, max_value=100.0,
                                      value=float(t.get("max_scalein_eur", 0.0)),
                                      step=0.5, key="tr_scalein")
        entry_cutoff = st.number_input("Entry cutoff (min voor window)", min_value=0, max_value=30,
                                       value=int(t.get("entry_cutoff_minutes_before_window", 2)),
                                       step=1, key="tr_cutoff")
        max_cost = st.number_input("Max gecombineerde cost (YES+NO ask)", min_value=0.90, max_value=1.10,
                                   value=float(e.get("max_combined_cost", 1.02)),
                                   step=0.01, key="tr_maxcost", format="%.2f")

    if st.button("Straddle opslaan", key="save_trading"):
        err = _save_sections({
            "trading": {
                "trade_size_eur": size,
                "max_scalein_eur": max_scalein,
                "trigger_threshold": trigger,
                "entry_start_minutes_before_window": entry_start,
                "entry_cutoff_minutes_before_window": entry_cutoff,
            },
            "entry": {"max_combined_cost": max_cost},
        })
        if err:
            _err(err)
        else:
            try:
                from src.commands import write_command
                write_command("set_trade_size", {"size": size})
            except Exception:
                pass
            _ok()
        _restart_hint()


def _section_exit(cfg: dict) -> None:
    st.subheader("Exit strategie")
    ex = cfg.get("exit", {})

    col1, col2 = st.columns(2)
    with col1:
        force_secs = st.number_input("Force exit (seconden voor einde)", min_value=5, max_value=120,
                                     value=int(ex.get("force_exit_seconds", 30)),
                                     step=5, key="ex_force")
        hold_thr = st.slider("Hold-for-resolution drempel (mid)", min_value=0.50, max_value=0.95,
                             value=float(ex.get("hold_for_resolution_mid_threshold", 0.55)),
                             step=0.01, key="ex_hold", format="%.2f")
        cross = st.slider("Peg-cross drempel (patient fase)", min_value=0.50, max_value=1.00,
                          value=float(ex.get("cross_threshold", 0.72)),
                          step=0.01, key="ex_cross", format="%.2f")
    with col2:
        ratchet = st.number_input("Ratchet buffer (¢)", min_value=0.01, max_value=0.15,
                                  value=float(ex.get("ratchet_buffer", 0.03)),
                                  step=0.005, key="ex_ratchet", format="%.3f")
        initial_offset = st.number_input("Initiële limit offset", min_value=0.01, max_value=0.20,
                                         value=float(ex.get("initial_offset", 0.07)),
                                         step=0.01, key="ex_offset", format="%.2f")
        hold_ev = st.toggle("Hold alleen als EV-positief", value=bool(ex.get("hold_for_resolution_ev_floor", True)),
                            key="ex_hold_ev")

    if st.button("Exit opslaan", key="save_exit"):
        err = _save_sections({"exit": {
            "force_exit_seconds": force_secs,
            "hold_for_resolution_mid_threshold": hold_thr,
            "cross_threshold": cross,
            "ratchet_buffer": ratchet,
            "initial_offset": initial_offset,
            "hold_for_resolution_ev_floor": hold_ev,
        }})
        if err:
            _err(err)
        else:
            _ok()
        _restart_hint()


def _section_trading_hours(cfg: dict) -> None:
    st.subheader("Trading uren (UTC)")
    th = cfg.get("trading_hours", {})

    enabled = st.toggle("Trading uren filter actief", value=bool(th.get("enabled", True)),
                        key="th_enabled")

    ALL_HOURS = list(range(24))
    skip = th.get("skip_utc_hours", [])
    # Show a multiselect of hours to SKIP
    skip_sel = st.multiselect(
        "Skip deze UTC uren (handelen in deze uren is geblokkeerd)",
        options=ALL_HOURS,
        default=[h for h in skip if h in ALL_HOURS],
        format_func=lambda h: f"{h:02d}:00",
        key="th_skip",
    )

    st.caption("Data 27-05: uur 05 (41% WR, -€8.21) en 07 (43% WR, -€6.73) zijn consistent verliesgevend. "
               "Uren 02 (83%) en 04 (87%) zijn het beste.")

    if st.button("Trading uren opslaan", key="save_th"):
        err = _save_sections({"trading_hours": {
            "enabled": enabled,
            "skip_utc_hours": skip_sel,
        }})
        if err:
            _err(err)
        else:
            _ok()
        _restart_hint()


def _section_signal_trader(cfg: dict) -> None:
    st.subheader("Signal Trader")
    st_cfg = cfg.get("signal_trader", {})

    col1, col2 = st.columns(2)
    with col1:
        paper = st.toggle("Paper modus", value=bool(st_cfg.get("paper_mode", True)), key="sett_st_paper")
        conviction = st.slider("Conviction drempel", min_value=0.30, max_value=0.90,
                               value=float(st_cfg.get("conviction_threshold", 0.50)),
                               step=0.01, key="sett_st_conv", format="%.2f")
        size = st.number_input("Trade grootte (€)", min_value=0.1, max_value=500.0,
                               value=float(st_cfg.get("trade_size_eur", 10.0)),
                               step=1.0, key="sett_st_size")
    with col2:
        max_loss = st.number_input("Max dagelijks verlies (€)", min_value=0.0, max_value=500.0,
                                   value=float(st_cfg.get("max_daily_loss_eur", 50.0)),
                                   step=5.0, key="sett_st_maxloss")
        max_trades = st.number_input("Max trades per dag", min_value=1, max_value=1000,
                                     value=int(st_cfg.get("max_trades_per_day", 200)),
                                     step=10, key="sett_st_maxtrades")
        max_conc = st.number_input("Max gelijktijdige posities", min_value=1, max_value=50,
                                   value=int(st_cfg.get("max_concurrent_positions", 10)),
                                   step=1, key="sett_st_maxconc")

    st.markdown("**Per-coin**")
    st_coins = st_cfg.get("coins", {})
    coin_changes: dict[str, dict] = {}
    cc = st.columns(len(COINS))
    for i, coin in enumerate(COINS):
        c = st_coins.get(coin, {})
        with cc[i]:
            en = st.toggle(coin, value=bool(c.get("enabled", True)), key=f"sett_st_coin_{coin}")
            if en != c.get("enabled", True):
                coin_changes[coin] = {"enabled": en}

    if st.button("Signal Trader opslaan", key="sett_save_st"):
        try:
            from ruamel.yaml import YAML
            ry = YAML()
            ry.preserve_quotes = True
            ry.width = 4096
            with open(_CONFIG_PATH) as f:
                full = ry.load(f)
            st_section = full.setdefault("signal_trader", {})
            st_section["paper_mode"] = paper
            st_section["conviction_threshold"] = conviction
            st_section["trade_size_eur"] = size
            st_section["max_daily_loss_eur"] = max_loss
            st_section["max_trades_per_day"] = max_trades
            st_section["max_concurrent_positions"] = max_conc
            coins_sec = st_section.setdefault("coins", {})
            for coin, params in coin_changes.items():
                for k, v in params.items():
                    coins_sec.setdefault(coin, {})[k] = v
            with open(_CONFIG_PATH, "w") as f:
                ry.dump(full, f)
            try:
                from src.commands import write_command
                write_command("apply_signal_trader_config", {
                    "conviction_threshold": conviction,
                    "trade_size_eur": size,
                    "paper_mode": paper,
                })
            except Exception:
                pass
            _ok()
        except Exception as e:
            _err(str(e))
        _restart_hint()


def _section_bggdsb(cfg: dict) -> None:
    st.subheader("BGGDSB strategie")
    b = cfg.get("bggdsb", {})

    col1, col2 = st.columns(2)
    with col1:
        paper = st.toggle("Paper modus", value=bool(b.get("paper_mode", True)), key="bg_paper")
        budget = st.number_input("Window budget (€)", min_value=0.1, max_value=100.0,
                                 value=float(b.get("window_budget_eur", 2.0)),
                                 step=0.5, key="bg_budget")
        dom_ask_max = st.slider("Max instapprijs dominante kant", min_value=0.40, max_value=0.90,
                                value=float(b.get("dom_ask_max", 0.65)),
                                step=0.01, key="bg_domask", format="%.2f")
    with col2:
        flip_en = st.toggle("Flip ingeschakeld", value=bool(b.get("flip_enabled", True)), key="bg_flip")
        hedge_en = st.toggle("Hedge ingeschakeld", value=bool(b.get("hedge_price_trigger", 0.15) > 0),
                             key="bg_hedge")
        avg_down = st.toggle("Avg-down ingeschakeld", value=bool(b.get("avg_down_enabled", True)),
                             key="bg_avgdown")

    if st.button("BGGDSB opslaan", key="save_bggdsb"):
        err = _save_sections({"bggdsb": {
            "paper_mode": paper,
            "window_budget_eur": budget,
            "dom_ask_max": dom_ask_max,
            "flip_enabled": flip_en,
            "avg_down_enabled": avg_down,
        }})
        if err:
            _err(err)
        else:
            _ok()
        _restart_hint()


def _section_scalper(cfg: dict) -> None:
    st.subheader("Stoplicht Scalper (15m)")
    s = cfg.get("stoplicht_scalper", {})

    col1, col2 = st.columns(2)
    with col1:
        enabled = st.toggle("Ingeschakeld", value=bool(s.get("enabled", False)), key="sc_en")
        paper = st.toggle("Paper modus", value=bool(s.get("paper_mode", True)), key="sc_paper")
        coin = st.selectbox("Coin", COINS,
                            index=COINS.index(s.get("coin", "BTC")) if s.get("coin", "BTC") in COINS else 0,
                            key="sc_coin")
        size = st.number_input("Trade grootte (€)", min_value=0.1, max_value=50.0,
                               value=float(s.get("trade_size_eur", 1.0)),
                               step=0.5, key="sc_size")
    with col2:
        green_thr = st.slider("GROEN drempel (score)", min_value=0.30, max_value=0.90,
                              value=float(s.get("green_threshold", 0.60)),
                              step=0.01, key="sc_green", format="%.2f")
        hold_thr = st.slider("Hold drempel (mid winnende kant)", min_value=0.70, max_value=0.95,
                             value=float(s.get("hold_threshold_init", 0.88)),
                             step=0.01, key="sc_hold", format="%.2f")
        trail_act = st.number_input("Trailing activatie (¢)", min_value=1, max_value=20,
                                    value=int(s.get("trail_activate_cts", 5)),
                                    step=1, key="sc_trail_act")
        trail_buf = st.number_input("Trailing buffer (¢)", min_value=1, max_value=10,
                                    value=int(s.get("trail_buffer_cts", 2)),
                                    step=1, key="sc_trail_buf")

    st.caption(f"Markt filter: `{s.get('market_filter', 'btc-updown-5m')}` — "
               "Polymarket biedt alleen 5-minuten BTC UP/DOWN markten.")

    if st.button("Scalper opslaan", key="save_scalper"):
        err = _save_sections({"stoplicht_scalper": {
            "enabled": enabled,
            "paper_mode": paper,
            "coin": coin,
            "trade_size_eur": size,
            "green_threshold": green_thr,
            "hold_threshold_init": hold_thr,
            "trail_activate_cts": trail_act,
            "trail_buffer_cts": trail_buf,
            "market_filter": f"{coin.lower()}-updown-15m",
        }})
        if err:
            _err(err)
        else:
            _ok()
        _restart_hint()


def _section_oracle(cfg: dict) -> None:
    st.subheader("Oracle")
    o = cfg.get("oracle", {})

    col1, col2 = st.columns(2)
    with col1:
        enabled = st.toggle("Oracle ingeschakeld", value=bool(o.get("enabled", True)), key="or_en")
        hard_gate = st.toggle("Hard gate (blokkeer koude trades)", value=bool(o.get("hard_gate", False)),
                              key="or_hard")
        discord_confirm = st.toggle("Discord confirm vragen", value=bool(o.get("discord_confirm_enabled", True)),
                                    key="or_disc")
    with col2:
        temp_hard = st.number_input("Temperatuur hard block (<)", min_value=0, max_value=50,
                                    value=int(o.get("temperature_hard_block", 30)),
                                    step=5, key="or_temphard")
        fear_thr = st.number_input("Extreme fear drempel (≤)", min_value=0, max_value=30,
                                   value=int(o.get("extreme_fear_threshold", 15)),
                                   step=1, key="or_fear")
        greed_thr = st.number_input("Extreme greed drempel (≥)", min_value=70, max_value=100,
                                    value=int(o.get("extreme_greed_threshold", 92)),
                                    step=1, key="or_greed")

    if st.button("Oracle opslaan", key="save_oracle"):
        err = _save_sections({"oracle": {
            "enabled": enabled,
            "hard_gate": hard_gate,
            "discord_confirm_enabled": discord_confirm,
            "temperature_hard_block": temp_hard,
            "extreme_fear_threshold": fear_thr,
            "extreme_greed_threshold": greed_thr,
        }})
        if err:
            _err(err)
        else:
            _ok()
        _restart_hint()


def _section_raw_yaml() -> None:
    st.subheader("Volledige config.yaml (geavanceerd)")
    st.warning("Direct bewerken — fouten in YAML kunnen de bot crashen. "
               "Maak eerst een backup.", icon="⚠️")
    try:
        with open(_CONFIG_PATH) as f:
            raw = f.read()
    except Exception as e:
        st.error(f"Kan config.yaml niet lezen: {e}")
        return

    edited = st.text_area("config.yaml", value=raw, height=500, key="raw_yaml_area")

    col_save, col_dl = st.columns(2)
    with col_dl:
        st.download_button("Download backup", data=raw,
                           file_name="config_backup.yaml", mime="text/yaml",
                           key="dl_yaml")
    with col_save:
        if st.button("Opslaan (raw YAML)", type="primary", key="save_raw_yaml"):
            try:
                import yaml
                parsed = yaml.safe_load(edited)
                if not isinstance(parsed, dict):
                    st.error("Ongeldige YAML — geen dict op top level.")
                    return
            except Exception as e:
                st.error(f"YAML syntax fout: {e}")
                return
            try:
                with open(_CONFIG_PATH, "w") as f:
                    f.write(edited)
                _ok("config.yaml opgeslagen. Herstart de bot om alle wijzigingen te activeren.")
                _restart_hint()
            except Exception as e:
                _err(str(e))


# ── Hoofd panel ────────────────────────────────────────────────────────────────

def settings_panel() -> None:
    st.title("Instellingen")
    st.caption("Wijzigingen worden direct naar config.yaml geschreven. "
               "De bot verwerkt de meeste via het command-systeem (~1s). "
               "Herstart de bot voor structurele wijzigingen.")

    cfg = _load_yaml()

    tab_mode, tab_risk, tab_trading, tab_st, tab_scalper, tab_bggdsb, tab_oracle, tab_yaml = st.tabs([
        "🎮 Mode & Coins",
        "🛡️ Risk",
        "📈 Straddle",
        "🎯 Signal Trader",
        "🚦 Scalper",
        "🧠 BGGDSB",
        "🔮 Oracle",
        "⚙️ Ruwe YAML",
    ])

    with tab_mode:
        _section_mode_coins(cfg)

    with tab_risk:
        _section_risk(cfg)
        st.divider()
        _section_trading_hours(cfg)

    with tab_trading:
        _section_trading(cfg)
        st.divider()
        _section_exit(cfg)

    with tab_st:
        _section_signal_trader(cfg)

    with tab_scalper:
        _section_scalper(cfg)

    with tab_bggdsb:
        _section_bggdsb(cfg)

    with tab_oracle:
        _section_oracle(cfg)

    with tab_yaml:
        _section_raw_yaml()
