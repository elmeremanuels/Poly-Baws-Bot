"""Poly-Baws-Bot Streamlit dashboard."""
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

from src.config_loader import CONFIG
from src.db_sync import (
    get_bot_heartbeat_age,
    get_current_cycle,
    get_daily_pnl,
    get_hybrid_pending,
    get_latest_completed_cycle,
    get_latest_manual_analysis,
    get_latest_snapshot_for_trade,
    get_learn_coin_counts,
    get_open_trades,
    get_signal_accuracy_per_coin,
    get_signal_active_trades,
    get_signal_trades,
    get_signal_trades_paginated,
    get_phase_stats,
    get_portfolio_snapshot,
    get_recent_events,
    get_recent_trades,
    get_scanner_alerts,
    get_scanner_state,
    get_state,
    get_today_trade_count,
    get_router_trades,
    get_oracle_pattern_stats,
    get_oracle_signal_patterns,
    get_oracle_verdicts,
    get_whale_meta,
    get_whale_activity,
    get_whale_positions,
    get_whale_bot_overlap,
)
from src.commands import write_command, delete_hybrid_pending
from src.db_sync import delete_signal_trades
from src.risk import KILL_FLAG_PATH, get_kill_reason as _get_kill_reason_fn
from analytics_tab import analytics_panel
from signal_lab_tab import signal_lab_panel
from coin_protection_tab import coin_protection_panel
from bggdsb_tab import bggdsb_panel

COINS = list(CONFIG["coins"].keys())
COIN_EMOJI = {"BTC": "₿", "ETH": "Ξ", "SOL": "◎", "XRP": "✕", "DOGE": "Ð"}
MODES = ["paper_hybrid", "paper_auto", "live_hybrid", "live_auto", "live_learning", "signal_trader", "auto_router", "bggdsb_paper", "bggdsb_live"]
MODE_LABELS = {
    "paper_hybrid":   "paper_hybrid",
    "paper_auto":     "paper_auto",
    "live_hybrid":    "🔴 live_hybrid",
    "live_auto":      "🔴 live_auto",
    "live_learning":  "🔴 live_learning",
    "signal_trader":  "signal_trader",
    "auto_router":    "auto_router",
    "bggdsb_paper":   "🧠 BGGDSB paper",
    "bggdsb_live":    "🔴 BGGDSB live",
}
# Modes that use real money and require confirmation before switching
LIVE_MODES = {"live_hybrid", "live_auto", "live_learning", "bggdsb_live"}

st.set_page_config(
    page_title="Poly-Baws-Bot",
    page_icon="₿",
    layout="wide",
    initial_sidebar_state="expanded",
)

_REGIME_STYLE: dict[str, tuple[str, str]] = {
    "TRENDING":  ("rgba(16,185,129,0.15)", "#34d399"),   # green
    "BREAKOUT":  ("rgba(59,130,246,0.15)", "#93c5fd"),   # blue
    "CHOPPY":    ("rgba(245,158,11,0.15)", "#fbbf24"),   # amber
    "RANGING":   ("rgba(139,92,246,0.15)", "#c4b5fd"),   # purple
    "NORMAL":    ("rgba(55,65,81,0.20)",   "#9ca3af"),   # gray
    "UNKNOWN":   ("rgba(55,65,81,0.10)",   "#4b5563"),   # dark gray
}

st.markdown("""
<style>
[data-testid="stAppViewContainer"] { background: #030712; }
[data-testid="stSidebar"] { background: #0f172a; }
.metric-card {
    background: #111827;
    border: 1px solid #1f2937;
    border-radius: 10px;
    padding: 12px 16px;
    margin-bottom: 8px;
}
.coin-card {
    background: #111827;
    border: 1px solid #1f2937;
    border-radius: 12px;
    padding: 14px;
}
.alert-banner {
    background: rgba(127,29,29,0.3);
    border: 1px solid rgba(185,28,28,0.5);
    border-radius: 8px;
    padding: 8px 14px;
    margin-bottom: 6px;
    color: #fca5a5;
    font-size: 13px;
}
.status-ok { color: #34d399; font-weight: 600; }
.status-warn { color: #fbbf24; font-weight: 600; }
.status-err { color: #f87171; font-weight: 600; }
.hybrid-panel {
    background: rgba(120,53,15,0.2);
    border: 1px solid rgba(180,83,9,0.4);
    border-radius: 10px;
    padding: 14px;
    margin-bottom: 12px;
}
div[data-testid="stMetricValue"] { color: #e5e7eb; }
.status-chip {
    display: inline-block;
    border-radius: 6px;
    padding: 3px 10px;
    font-size: 12px;
    font-weight: 600;
    margin-bottom: 6px;
}
.chip-wait  { background: rgba(55,65,81,0.6);  color: #9ca3af; }
.chip-soon  { background: rgba(120,53,15,0.4); color: #fbbf24; }
.chip-entry { background: rgba(6,78,59,0.5);   color: #34d399; }
.chip-trade { background: rgba(30,58,138,0.5); color: #93c5fd; }
.chip-none  { background: rgba(127,29,29,0.4); color: #fca5a5; }
.chip-warn  { background: rgba(120,53,15,0.4); color: #fbbf24; }
@keyframes fadeRefresh {
  from { opacity: 0.80; }
  to   { opacity: 1.0;  }
}
[data-testid="stVerticalBlockInsideMain"] > div {
  animation: fadeRefresh 0.35s ease-out;
}
[data-testid="stMetricValue"] > div,
[data-testid="stMetricDelta"] span {
  transition: color 0.4s ease, opacity 0.25s ease;
}
</style>
""", unsafe_allow_html=True)


# ── Tab loading placeholder ────────────────────────────────────────────────────

def _tab_loading(title: str, subtitle: str = "Data wordt opgehaald…") -> None:
    """Pulsing skeleton shown while a tab's content is still initialising."""
    st.markdown(f"""
<div style="display:flex;flex-direction:column;align-items:center;
            justify-content:center;padding:80px 20px;color:#6b7280">
  <svg width="36" height="36" viewBox="0 0 24 24" fill="none"
       stroke="#3b82f6" stroke-width="2" stroke-linecap="round"
       style="animation:spin 1.2s linear infinite">
    <path d="M21 12a9 9 0 1 1-6.219-8.56"/>
  </svg>
  <p style="font-size:16px;font-weight:600;color:#9ca3af;margin:16px 0 4px">{title}</p>
  <p style="font-size:12px;color:#4b5563;margin:0">{subtitle}</p>
</div>
<style>
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
</style>
""", unsafe_allow_html=True)


# ── Helpers ────────────────────────────────────────────────────────────────────

def current_mode() -> str:
    return get_state("mode") or "paper_hybrid"


def is_killed() -> bool:
    return KILL_FLAG_PATH.exists()


def bot_status_html() -> str:
    age = get_bot_heartbeat_age()
    if is_killed():
        return '<span class="status-err">🛑 KILLED — geen trades</span>'
    if age is None:
        return '<span class="status-warn">⚠ Bot niet gestart</span>'
    if age < 15:
        return '<span class="status-ok">● Actief</span>'
    mins = int(age // 60)
    secs = int(age % 60)
    since = f"{mins}m {secs}s" if mins else f"{secs}s"
    return f'<span class="status-err">⚠ Bot gestopt ({since} geleden)</span>'


def bot_is_online() -> bool:
    age = get_bot_heartbeat_age()
    return age is not None and age < 30


def fmt_eur(v: float) -> str:
    return f"{'+'if v >= 0 else ''}€{v:.2f}"


def _coin_status(coin: str, open_trades: list[dict], online: bool = True) -> tuple[str, str]:
    """Return (chip_class, label) for a coin's current scanner/trade state."""
    cfg = CONFIG["trading"]
    start_before = cfg["entry_start_minutes_before_window"]
    cutoff_before = cfg["entry_cutoff_minutes_before_window"]
    now = datetime.now(timezone.utc)

    in_trade = [t for t in open_trades if t.get("coin") == coin]
    if in_trade:
        return "chip-trade", f"🔵 In trade ({len(in_trade)})"

    if not online:
        return "chip-warn", "⚠ Bot offline"

    data = get_scanner_state(coin)
    count = data.get("count", 0)
    if count == 0:
        return "chip-none", "🔴 Geen markten"

    next_start = data.get("next_start")
    if not next_start:
        return "chip-wait", f"⏳ {count} markten"

    try:
        ws = datetime.fromisoformat(next_start)
        mins = (ws - now).total_seconds() / 60
    except Exception:
        return "chip-wait", f"⏳ {count} markten"

    if mins < 0:
        return "chip-wait", "⏳ In window"
    elif mins < cutoff_before:
        return "chip-wait", f"⏳ Te laat ({mins:.0f}m)"
    elif mins <= start_before:
        return "chip-entry", f"🟢 ENTRY ({mins:.0f}m)"
    else:
        h, m = divmod(int(mins), 60)
        label = f"⏳ {h}u {m:02d}m" if h else f"⏳ {m}m"
        return "chip-wait", label


_LOCAL_TZ = ZoneInfo("Europe/Amsterdam")


def _to_local(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_LOCAL_TZ)


def fmt_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return _to_local(iso).strftime("%H:%M")
    except Exception:
        return iso[:16]


# ── Live mode confirmation dialog ─────────────────────────────────────────────

@st.dialog("⚠️ Live modus activeren — echt geld!")
def _live_confirm_dialog(new_mode: str) -> None:
    label = MODE_LABELS.get(new_mode, new_mode)
    st.error(f"Je staat op het punt **{label}** te activeren.", icon="🔴")

    st.markdown("**Actieve instellingen:**")

    if new_mode == "bggdsb_live":
        budget = int(get_state("bggdsb_window_budget") or 20)
        avg_down = round(budget * 4.00, 2)
        flip     = round(budget * 3.00, 2)
        confirm  = round(budget * 1.00, 2)
        max_tot  = round(budget + avg_down + flip + confirm + budget * 0.10, 2)
        c1, c2 = st.columns(2)
        c1.metric("Budget per window", f"€{budget}")
        c2.metric("Max totaal per window", f"€{max_tot}")
        coins_raw = get_state("bggdsb_coins")
        try:
            import json as _json
            coins = ", ".join(_json.loads(coins_raw)) if coins_raw else "?"
        except Exception:
            coins = "?"
        st.info(f"Coins: **{coins}**")
    else:
        saved_eur = get_state("trade_size_eur")
        trade_size = float(saved_eur) if saved_eur else CONFIG["trading"].get("trade_size_eur", 1.0)
        saved_sc = get_state("max_scalein_eur")
        scalein = float(saved_sc) if saved_sc else CONFIG["trading"].get("max_scalein_eur", 0.0)
        daily_limit = CONFIG.get("risk", {}).get("daily_loss_limit_eur", 10.0)
        c1, c2, c3 = st.columns(3)
        c1.metric("Trade size", f"€{trade_size:.2f}")
        c2.metric("Max bijkoop", f"€{scalein:.2f}" if scalein > 0 else "uit")
        c3.metric("Dagelijkse stop", f"€{daily_limit:.2f}")

    st.divider()
    col_ok, col_cancel = st.columns(2)
    if col_ok.button("✅ Ja, ga live", type="primary", use_container_width=True):
        write_command("set_mode", {"mode": new_mode})
        st.session_state.pop("_pending_live_mode", None)
        st.rerun()
    if col_cancel.button("❌ Annuleren", use_container_width=True):
        # Reset the radio widget back to the current (paper) mode
        cur = current_mode()
        if cur in MODES:
            st.session_state["sidebar_mode_radio"] = MODES.index(cur)
        st.session_state.pop("_pending_live_mode", None)
        st.rerun()


# ── Sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## ₿ Poly-Baws-Bot")
    st.markdown(bot_status_html(), unsafe_allow_html=True)
    st.divider()

    st.markdown("**Mode**")
    mode = current_mode()
    mode_idx = MODES.index(mode) if mode in MODES else 0
    new_mode = st.radio(
        "mode", MODES,
        index=mode_idx,
        format_func=lambda m: MODE_LABELS.get(m, m),
        label_visibility="collapsed",
        key="sidebar_mode_radio",
    )
    if new_mode != mode:
        # Ignore bggdsb paper↔live discrepancy: the BGGDSB tab manages this switch.
        # The radio session state may lag behind an external "Go LIVE" button click;
        # blocking the revert here prevents the sidebar from undoing it.
        _bggdsb_modes = {"bggdsb_paper", "bggdsb_live"}
        if new_mode in _bggdsb_modes and mode in _bggdsb_modes:
            pass  # don't send command — BGGDSB tab is authoritative for paper↔live
        elif new_mode in LIVE_MODES:
            # Show confirmation dialog instead of switching immediately
            st.session_state["_pending_live_mode"] = new_mode
        else:
            write_command("set_mode", {"mode": new_mode})
            st.rerun()

    # Open live confirmation dialog if pending
    if "_pending_live_mode" in st.session_state:
        _live_confirm_dialog(st.session_state["_pending_live_mode"])

    if mode == "live_auto":
        completed = get_latest_completed_cycle()
        if completed:
            conf = completed.get("confidence_score")
            cycle_num = completed.get("cycle_number", "?")
            label = (
                f"Apply learnings  \n"
                f"_Cycle #{cycle_num}"
                + (f", {conf*100:.0f}% confidence_" if conf is not None else "_")
            )
            apply_active = get_state("apply_learnings") == "true"
            apply_new = st.toggle(label, value=apply_active, key="apply_learnings_toggle")
            if apply_new != apply_active:
                write_command("toggle_learned_params", {"enabled": apply_new})
                st.rerun()
        else:
            st.caption("_Geen learnings beschikbaar — voer eerst live\\_learning uit._")

    if mode.startswith("paper") or mode.endswith("_paper"):
        confirmed = st.checkbox("Bevestig reset", key="reset_confirm")
        if st.button("🗑 Reset paper data", use_container_width=True, disabled=not confirmed):
            write_command("reset_paper")
            st.session_state.pop("reset_confirm", None)
            st.toast("Paper data gereset.", icon="🗑")
            st.rerun()

    st.divider()

    heartbeat_age = get_bot_heartbeat_age()
    process_alive = heartbeat_age is not None and heartbeat_age < 15
    killed = is_killed()

    st.markdown("### Bot Power")
    if not process_alive:
        if heartbeat_age is not None:
            st.error(f"❌ BOT GESTOPT — laatste heartbeat {heartbeat_age:.0f}s geleden")
        else:
            st.error("❌ BOT NOOIT GESTART")
        st.caption("Check: sudo systemctl status poly-baws-bot")
    elif killed:
        _kill_reason = _get_kill_reason_fn()
        if _kill_reason:
            st.error(f"⛔ Gestopt: `{_kill_reason}`")
        st.warning("⏸ BOT GEPAUZEERD — draait maar handelt niet")
        if st.button("▶ RESUME TRADING", use_container_width=True, type="primary"):
            # Delete kill flag directly (instant) AND queue command for bot's in-memory flag
            if KILL_FLAG_PATH.exists():
                KILL_FLAG_PATH.unlink()
            write_command("reset_kill")
            st.rerun()
    else:
        st.success("🟢 BOT LIVE — trading actief")
        if st.button("⏸ PAUSE TRADING", use_container_width=True, type="secondary"):
            write_command("kill")
            st.rerun()

    st.divider()

    if mode == "auto_router":
        saved_eur = get_state("trade_size_eur")
        db_eur = float(saved_eur) if saved_eur else CONFIG["trading"].get("trade_size_eur", 1.0)
        st_size = CONFIG.get("signal_trader", {}).get("trade_size_eur", 10.0)
        st.markdown("**Trade groottes**")
        st.caption(f"Straddle: **€{db_eur:.2f}** · Signal: **€{st_size:.2f}**")
        st.info("Pas aan via het **🤖 Auto Router** tabblad.", icon="ℹ️")
    else:
        st.markdown("**Trade size (EUR per leg)**")
        saved_eur = get_state("trade_size_eur")
        db_eur = float(saved_eur) if saved_eur else CONFIG["trading"].get("trade_size_eur", 1.0)
        _ts_key = "_pending_trade_size_eur"
        pending = st.session_state.get(_ts_key)
        if pending is not None and abs(pending - db_eur) < 0.001:
            del st.session_state[_ts_key]
        display_eur = st.session_state.get(_ts_key, db_eur)
        _ts_disabled = mode in ("bggdsb_paper", "bggdsb_live")
        new_eur = st.number_input(
            "trade_size_input",
            min_value=0.50,
            max_value=100.0,
            value=display_eur,
            step=0.10,
            format="%.2f",
            label_visibility="collapsed",
            disabled=_ts_disabled,
            help="Niet van toepassing in BGGDSB modus — pas aan via het 🧠 BGGDSB tabblad." if _ts_disabled else None,
        )
        if abs(new_eur - display_eur) > 0.001:
            write_command("set_trade_size", {"trade_size_eur": new_eur})
            st.session_state[_ts_key] = new_eur
            st.rerun()

        st.markdown("**Max bijkoop per positie (EUR)**")
        st.caption("Totaal bijkoop-budget, gespreid over 5 tranches op bevestigd herstel (TRENDING/NORMAL). 0 = uit, anders minimaal €5.")
        saved_scalein = get_state("max_scalein_eur")
        db_scalein = float(saved_scalein) if saved_scalein else CONFIG["trading"].get("max_scalein_eur", 0.0)
        _sc_key = "_pending_max_scalein_eur"
        pending_sc = st.session_state.get(_sc_key)
        if pending_sc is not None and abs(pending_sc - db_scalein) < 0.001:
            del st.session_state[_sc_key]
        display_scalein = st.session_state.get(_sc_key, db_scalein)
        _sc_disabled = mode in ("bggdsb_paper", "bggdsb_live", "signal_trader", "auto_router")
        new_scalein = st.number_input(
            "max_scalein_input",
            min_value=0.0,
            max_value=100.0,
            value=display_scalein,
            step=0.10,
            format="%.2f",
            label_visibility="collapsed",
            disabled=_sc_disabled,
            help="Niet van toepassing in deze modus." if _sc_disabled else None,
        )
        if abs(new_scalein - display_scalein) > 0.001:
            write_command("set_max_scalein", {"max_scalein_eur": new_scalein})
            st.session_state[_sc_key] = new_scalein
            st.rerun()

    st.divider()

    daily_pnl = get_daily_pnl()
    total_trades = get_today_trade_count()
    st.metric("Today's P&L", fmt_eur(daily_pnl))
    st.metric("Today's trades", total_trades)

    st.divider()

    _pf = get_portfolio_snapshot()
    if _pf["value"] is not None:
        st.markdown("**Portfolio**")
        st.metric("USDC", f"${_pf['usdc']:.2f}" if _pf["usdc"] is not None else "—")
        st.metric("Totaal", f"${_pf['value']:.2f}")
        if _pf["start_usdc"] is not None:
            _pnl = (_pf["value"] or 0) - _pf["start_usdc"]
            st.metric("P&L vs start", f"${_pnl:+.2f}")
        if _pf["updated_at"]:
            st.caption(f"Bijgewerkt: {fmt_time(_pf['updated_at'])}")
        st.divider()

    st.caption("Auto-refreshes every 5s")


# ── Signal strategy status bar ────────────────────────────────────────────────

def _signal_strategy_bar() -> None:
    """Compact status + manual toggles for directional_entry and conviction_weighting."""
    de_enabled_str = get_state("directional_entry_enabled")
    cw_enabled_str = get_state("conviction_weighting_enabled")
    wg_cfg = CONFIG.get("weighting_guard", {})
    de_cfg = CONFIG.get("directional_entry", {})
    cw_cfg = CONFIG.get("conviction_weighting", {})

    # Live state: prefer DB value (bot may have toggled it) else fall back to config.yaml
    de_on = (de_enabled_str.lower() == "true") if de_enabled_str else de_cfg.get("enabled", False)
    cw_on = (cw_enabled_str.lower() == "true") if cw_enabled_str else cw_cfg.get("enabled", False)

    de_rate_str = get_state("wg_de_last_rate")
    cw_rate_str = get_state("wg_cw_last_rate")
    de_auto = get_state("wg_de_auto_disabled") == "true"
    cw_auto = get_state("wg_cw_auto_disabled") == "true"

    guard_on = wg_cfg.get("enabled", True)
    disable_thr = wg_cfg.get("disable_threshold", 0.47)
    enable_thr = wg_cfg.get("enable_threshold", 0.53)

    def _accuracy_chip(rate_str: str | None, on: bool) -> str:
        if rate_str is None:
            return "geen data"
        rate = float(rate_str)
        pct = f"{rate * 100:.0f}%"
        if rate < disable_thr:
            return f"🔴 {pct}"
        if rate >= enable_thr:
            return f"🟢 {pct}"
        return f"🟡 {pct}"

    with st.expander(
        f"⚡ Signaalstrategieën — "
        f"Directional: {'🟢 AAN' if de_on else '🔴 UIT'}  |  "
        f"Gewogen inkoop: {'🟢 AAN' if cw_on else '🔴 UIT'}",
        expanded=False,
    ):
        st.caption(
            f"Auto-beheer: {'✅ actief' if guard_on else '❌ uit'}  ·  "
            f"Drempels: uitschakel <{int(disable_thr*100)}%, inschakelen ≥{int(enable_thr*100)}%"
        )
        col_de, col_cw = st.columns(2)

        with col_de:
            st.markdown("**Directional Inkoop** *(enkel YES of NO)*")
            st.caption(
                f"Status: {'🟢 AAN' if de_on else '🔴 UIT'}"
                + (" *(auto-uit)*" if de_auto else "")
                + f"  |  Nauwkeurigheid: {_accuracy_chip(de_rate_str, de_on)}"
            )
            col_de_on, col_de_off = st.columns(2)
            with col_de_on:
                if st.button("▶ AAN", key="sg_de_on", disabled=de_on):
                    write_command("set_directional_entry", {"enabled": True})
                    st.rerun()
            with col_de_off:
                if st.button("⏹ UIT", key="sg_de_off", disabled=not de_on):
                    write_command("set_directional_entry", {"enabled": False})
                    st.rerun()

        with col_cw:
            st.markdown("**Gewogen Inkoop** *(asymmetrische maten)*")
            st.caption(
                f"Status: {'🟢 AAN' if cw_on else '🔴 UIT'}"
                + (" *(auto-uit)*" if cw_auto else "")
                + f"  |  Nauwkeurigheid: {_accuracy_chip(cw_rate_str, cw_on)}"
            )
            col_cw_on, col_cw_off = st.columns(2)
            with col_cw_on:
                if st.button("▶ AAN", key="sg_cw_on", disabled=cw_on):
                    write_command("set_conviction_weighting", {"enabled": True})
                    st.rerun()
            with col_cw_off:
                if st.button("⏹ UIT", key="sg_cw_off", disabled=not cw_on):
                    write_command("set_conviction_weighting", {"enabled": False})
                    st.rerun()


# ── Main dashboard (auto-refresh every 5s) ────────────────────────────────────

@st.fragment(run_every=5)
def _dashboard_live() -> None:
    _scanner_alerts()
    _signal_strategy_bar()
    _hybrid_panel()
    open_trades = get_open_trades()
    _coin_grid(open_trades)
    st.divider()
    col_left, col_right = st.columns(2)
    with col_left:
        _active_positions(open_trades)
    with col_right:
        _recent_trades()


@st.fragment
def dashboard() -> None:
    _dashboard_live()


@st.fragment(run_every=5)
def _event_log_live() -> None:
    st.divider()
    _event_log()


@st.fragment
def dashboard_event_log() -> None:
    _event_log_live()


def _scanner_alerts() -> None:
    alerts = get_scanner_alerts(5)
    now = datetime.now(timezone.utc)
    shown = 0
    for a in alerts:
        ts_str = a.get("ts") or ""
        try:
            ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
            if (now - ts_dt).total_seconds() > 1800:  # ignore alerts older than 30 min
                continue
        except Exception:
            continue
        try:
            data = json.loads(a.get("data") or "{}")
        except Exception:
            data = {}
        coin = a.get("coin") or "?"
        reason = data.get("reason", "unknown")
        st.markdown(
            f'<div class="alert-banner">🔴 <b>SCANNER ALERT</b> — {coin}: {reason}'
            f' <span style="color:#7f1d1d;float:right">{_to_local(ts_str).strftime("%Y-%m-%d %H:%M:%S") if ts_str else ""}</span></div>',
            unsafe_allow_html=True,
        )
        shown += 1
        if shown >= 3:
            break


def _hybrid_panel() -> None:
    mode = current_mode()
    if "hybrid" not in mode:
        return
    pending = get_hybrid_pending()
    if not pending:
        return

    st.markdown("### 🎯 Hybrid Trigger Panel")
    for win in pending:
        market_id = win.get("market_id", "")
        coin = win.get("coin", "?")
        w_start = fmt_time(win.get("window_start"))
        question = win.get("question") or market_id

        col1, col2 = st.columns([4, 1])
        with col1:
            st.markdown(
                f'<div class="hybrid-panel">'
                f'<b>{COIN_EMOJI.get(coin, "")} {coin}</b> · {w_start}<br>'
                f'<span style="color:#9ca3af;font-size:12px">{question[:80]}</span>'
                f"</div>",
                unsafe_allow_html=True,
            )
        with col2:
            btn_key = f"trigger_{market_id}"
            if st.button("▶ Trigger", key=btn_key, type="primary", use_container_width=True):
                write_command("trigger_hybrid", {"market_id": market_id})
                st.toast(f"Entry triggered: {coin} {w_start}", icon="🚀")


def _coin_grid(open_trades: list | None = None) -> None:
    st.markdown("### Coins")
    if open_trades is None:
        open_trades = get_open_trades()
    online = bot_is_online()
    cols = st.columns(5)
    for i, coin in enumerate(COINS):
        with cols[i]:
            _coin_card(coin, open_trades, online)


def _regime_chip_html(coin: str) -> str:
    """Render a small regime badge using saved dashboard_state."""
    raw = get_state(f"regime_{coin}")
    if not raw:
        return ""
    try:
        import json as _j
        data = _j.loads(raw)
    except Exception:
        return ""
    regime = data.get("regime", "UNKNOWN")
    bias = data.get("bias")
    rng = data.get("range_pct")
    bg, fg = _REGIME_STYLE.get(regime, _REGIME_STYLE["UNKNOWN"])
    label = regime
    if bias:
        label += f" {'↑' if bias == 'UP' else '↓'}"
    tooltip = regime
    if rng is not None:
        tooltip += f" · range {rng:.1f}%"
    return (
        f'<span title="{tooltip}" style="'
        f'background:{bg};color:{fg};'
        f'border-radius:5px;padding:2px 7px;font-size:11px;font-weight:600;'
        f'margin-left:4px;vertical-align:middle">{label}</span>'
    )


def _coin_card(coin: str, open_trades: list[dict], online: bool = True) -> None:
    cfg = CONFIG["coins"][coin]
    pnl = get_daily_pnl(coin)
    count = get_today_trade_count(coin)

    enabled_str = get_state(f"coin_{coin}_enabled")
    enabled = cfg["enabled"] if enabled_str is None else (enabled_str.lower() == "true")
    max_str = get_state(f"coin_{coin}_max")
    max_p = int(max_str) if max_str else cfg["max_parallel_positions"]

    ss_max_key = f"sent_max_{coin}"
    ss_en_key = f"sent_en_{coin}"
    display_max = st.session_state.get(ss_max_key, max_p)
    display_en = st.session_state.get(ss_en_key, enabled)

    pnl_color = "#34d399" if pnl >= 0 else "#f87171"
    chip_cls, chip_label = _coin_status(coin, open_trades, online)

    st.markdown(
        f'<div style="margin-bottom:4px">'
        f'<span style="font-weight:700;font-size:15px">{COIN_EMOJI.get(coin,"")} {coin}</span>'
        f'{_regime_chip_html(coin)}'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.markdown(f'<div class="status-chip {chip_cls}">{chip_label}</div>', unsafe_allow_html=True)

    new_enabled = st.toggle("Enabled", value=display_en, key=f"en_{coin}")
    if new_enabled != display_en:
        write_command("set_coin_config", {"coin": coin, "enabled": new_enabled})
        st.session_state[ss_en_key] = new_enabled
        st.rerun()

    new_max = st.slider("Max pos.", 0, 5, display_max, key=f"mx_{coin}")
    if new_max != display_max:
        write_command("set_coin_config", {"coin": coin, "max_parallel": new_max})
        st.session_state[ss_max_key] = new_max

    st.markdown(
        f'<p style="color:{pnl_color};margin:4px 0">{fmt_eur(pnl)}</p>'
        f'<p style="color:#6b7280;font-size:12px">{count} trades</p>',
        unsafe_allow_html=True,
    )
    # Coin guard status badge — beheer via het Beveiliging-tabblad
    _guard_state = get_state(f"cg_{coin}_state") or "active"
    _guard_streak = get_state(f"cg_{coin}_streak") or "0"
    _paper_wins_n = get_state(f"cg_{coin}_paper_wins") or "0"
    if _guard_state == "disabled":
        if not st.session_state.get(f"_gd_card_{coin}"):
            _ga, _gb = st.columns([5, 1])
            with _ga:
                st.error("🚫 Guard — zie Beveiliging", icon=None)
            with _gb:
                if st.button("✕", key=f"gd_card_x_{coin}", help="Verberg"):
                    st.session_state[f"_gd_card_{coin}"] = True
                    st.rerun()
    elif _guard_state == "paper_only":
        _needed = CONFIG.get("coin_guard", {}).get("paper_only_recovery_wins", 3)
        st.info(
            f"📄 Paper-only herstel: {_paper_wins_n}/{_needed} wins\n\n"
            f"Na {_needed} opeenvolgende paper-wins gaat deze coin terug naar live.",
            icon=None,
        )
    elif _guard_state == "watch":
        st.warning(f"⚠️ Watch: {_guard_streak}× verlies op rij")


@st.dialog("Trade Details", width="large")
def _trade_detail_dialog(trade: dict) -> None:
    coin = trade.get("coin", "")
    status = trade.get("status", "")
    trade_mode = (trade.get("mode") or "").replace("_", " ")
    trade_id = trade.get("trade_id", "")
    winner_side = trade.get("winner_side")

    st.markdown(f"### {COIN_EMOJI.get(coin, '')} {coin} — {status.upper()}")
    st.caption(f"Mode bij entry: **{trade_mode}** · ID: `{trade_id[:12]}…`")
    st.divider()

    entry_yes = trade.get("entry_yes_price")
    entry_no = trade.get("entry_no_price")
    be = trade.get("break_even_price")

    exit_cfg = CONFIG.get("exit", {})
    if be is not None:
        target = round(be + exit_cfg.get("min_winner_profit_margin", 0.03), 3)
    else:
        trigger_thr = CONFIG["trading"].get("trigger_threshold", 0.73)
        target = round(trigger_thr + exit_cfg.get("initial_offset", 0.05), 3)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Entry YES", f"{entry_yes:.3f}" if entry_yes else "—")
    c2.metric("Entry NO", f"{entry_no:.3f}" if entry_no else "—")
    c3.metric("Break-even", f"{be:.3f}" if be is not None else "—")
    c4.metric("Doelprijs", f"{target:.3f}")

    if winner_side:
        st.success(f"Trigger geraakt — **{winner_side}** heeft gewonnen")

    # Latest prices from orderbook snapshot (written every 30s during monitoring)
    snap = get_latest_snapshot_for_trade(trade_id)
    if snap:
        yes_mid = snap.get("yes_mid")
        no_mid = snap.get("no_mid")
        age = snap.get("age_seconds")
        age_str = f"{age:.0f}s geleden" if age is not None else "onbekend"
        st.markdown(f"**Live prijzen** _(snapshot {age_str})_")
        cs1, cs2, cs3 = st.columns(3)
        cs1.metric("YES mid (kans omhoog)", f"{yes_mid:.3f}" if yes_mid else "—")
        cs2.metric("NO mid (kans omlaag)", f"{no_mid:.3f}" if no_mid else "—")
        if yes_mid is not None:
            conf = yes_mid if (winner_side or "YES") == "YES" else (1 - yes_mid)
            cs3.metric("Polymarket confidence", f"{conf*100:.1f}%")
    else:
        st.caption("Nog geen prijssnapshot — trade is pas gestart of monitoring nog niet begonnen.")

    st.divider()
    c5, c6 = st.columns(2)
    c5.metric("Window start", fmt_time(trade.get("window_start_ts")))
    c6.metric("Window einde", fmt_time(trade.get("window_end_ts")))

    yes_token = trade.get("condition_id_yes", "")
    no_token = trade.get("condition_id_no", "")
    if yes_token:
        st.caption(f"YES token: `{yes_token[:24]}…`")
    if no_token:
        st.caption(f"NO token: `{no_token[:24]}…`")


def _active_positions(open_trades: list | None = None) -> None:
    st.markdown("#### Active Positions")
    trades = open_trades if open_trades is not None else get_open_trades()
    if not trades:
        st.caption("No active positions.")
        return

    for t in trades:
        be = t.get("break_even_price")
        trade_id = t.get("trade_id", "")
        coin = t.get("coin", "")
        status = t.get("status", "")
        winner = t.get("winner_side") or "—"
        trade_mode = (t.get("mode") or "").replace("_", " ")

        c = st.columns([1, 1.5, 1.5, 1.5, 1.5, 1.5, 1.5, 1, 1])
        c[0].markdown(f"**{coin}**")
        c[1].caption(status)
        c[2].caption(trade_mode)
        c[3].caption(fmt_time(t.get("window_start_ts")))
        c[4].caption(f"YES {t['entry_yes_price']:.2f}" if t.get("entry_yes_price") else "YES —")
        c[5].caption(f"NO {t['entry_no_price']:.2f}" if t.get("entry_no_price") else "NO —")
        c[6].caption(f"BE €{be:.2f}" if be is not None else "BE —")
        if c[7].button("ℹ️", key=f"info_{trade_id}", help="Details bekijken"):
            st.session_state["_detail_trade_id"] = trade_id
            st.rerun(scope="app")  # full page rerun so dialog renders outside fragment
        if c[8].button("🛑", key=f"fc_{trade_id}", help="Force close deze positie"):
            write_command("force_close_trade", {"trade_id": trade_id})
            st.toast(f"{coin} force close verstuurd.", icon="🛑")
            st.rerun()


def _recent_trades() -> None:
    st.markdown("#### Recent Trades")
    trades = get_recent_trades(20)
    if not trades:
        st.caption("No trades yet.")
        return

    rows = []
    for t in trades:
        pnl = t.get("net_pnl")
        peak = t.get("peak_bid")
        be = t.get("break_even_price")
        rows.append({
            "Coin": t.get("coin", ""),
            "Exit": t.get("winner_exit_reason") or t.get("status", ""),
            "Break-even": f"€{be:.2f}" if be is not None else "—",
            "Peak bid": f"{peak:.2f}" if peak is not None else "—",
            "Ratchets": t.get("ratchet_count") or 0,
            "Mode": (t.get("mode") or "").replace("_", " "),
            "Net P&L": f"{'+' if (pnl or 0) >= 0 else ''}€{pnl:.4f}" if pnl is not None else "—",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _event_log() -> None:
    with st.expander("Event Log", expanded=False):
        filter_text = st.text_input("Filter", placeholder="event type, coin, trade_id…", key="log_filter")
        events = get_recent_events(200)
        if filter_text:
            events = [
                e for e in events
                if filter_text.lower() in json.dumps(e).lower()
            ]
        rows = []
        for e in events[:100]:
            rows.append({
                "Time": _to_local(e["ts"]).strftime("%Y-%m-%d %H:%M:%S") if e.get("ts") else "—",
                "Type": e.get("event_type", ""),
                "Coin": e.get("coin") or "—",
                "Data": (e.get("data") or "")[:120],
            })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.caption("No events.")


@st.fragment
def _learning_panel() -> None:
    cycle = get_current_cycle()

    if not cycle:
        st.info("Learning mode is not active. Set mode to **live_learning** to start.")
        if st.button("Start Learning Mode"):
            write_command("set_mode", {"mode": "live_learning"})
            st.rerun()
        return

    phase = cycle.get("phase", "?")
    phase_emoji = {"learn": "📚", "analyze": "🧠", "deploy": "🚀", "validate": "🔍"}
    phase_color = {"learn": "🟡", "analyze": "🔵", "deploy": "🟢", "validate": "🟠"}

    st.markdown(f"### {phase_color.get(phase, '⚪')} Learning Cycle #{cycle.get('cycle_number', '?')}")

    # ── LIVE vs LEARNING banner + projected progress ────────────────────────────
    # A phase ends at whichever limit hits first (time cap OR trade count), so we
    # surface both: a time-remaining bar (from max_*_hours) and a trade-count bar.
    lcfg = CONFIG["learning"]
    _phase_kind = {
        "learn":    ("📚 LEARNING (paper)",      "Data verzamelen — geen echt geld",      "max_learn_hours"),
        "analyze":  ("🧠 ANALYSE (Claude)",      "Claude analyseert de resultaten (~1-2 min)", None),
        "deploy":   ("🟢 LIVE TRADING",          "Echt geld — bot plaatst echte orders",  "max_live_hours"),
        "validate": ("🔍 LEARNING (validatie)",  "Paper — geleerde parameters toetsen",   "max_validate_hours"),
    }
    _label, _desc, _hours_key = _phase_kind.get(phase, (f"⚪ {phase.upper()}", "", None))
    _banner = f"**{_label}** — {_desc}"
    if phase == "deploy":
        st.success(_banner)
    elif phase == "analyze":
        st.info(_banner)
    else:
        st.warning(_banner)

    # Time-remaining bar (only for time-capped phases with a known phase start)
    _ps_raw = cycle.get("phase_started_at") or ""
    _elapsed_h = None
    if _ps_raw:
        try:
            _ps_dt = datetime.fromisoformat(_ps_raw).astimezone(timezone.utc)
            _elapsed_h = (datetime.now(timezone.utc) - _ps_dt).total_seconds() / 3600
        except Exception:
            _elapsed_h = None
    if _hours_key and _elapsed_h is not None:
        _max_h = float(lcfg.get(_hours_key, 0) or 0)
        if _max_h > 0:
            _rem_h = max(0.0, _max_h - _elapsed_h)
            _rh, _rm = int(_rem_h), int(round((_rem_h - int(_rem_h)) * 60))
            st.progress(min(1.0, _elapsed_h / _max_h),
                        text=f"⏱ Tijd-limiet: nog ~{_rh}u {_rm}m van {_max_h:.0f}u")

    # Trade-count bar (the other exit condition for this phase)
    _cid = cycle.get("id")
    if _cid:
        if phase == "learn":
            _counts = get_learn_coin_counts(_cid)
            _enabled = [c for c, v in CONFIG["coins"].items() if v.get("enabled", True)]
            _need = lcfg["min_trades_per_coin"]
            _covered = sum(1 for c in _enabled if _counts.get(c, 0) >= _need)
            st.progress(_covered / max(1, len(_enabled)),
                        text=f"📊 Coins gedekt: {_covered}/{len(_enabled)} (elk ≥{_need} paper-trades)")
            _missing = [f"{COIN_EMOJI.get(c, '')}{c} {_counts.get(c, 0)}/{_need}"
                        for c in _enabled if _counts.get(c, 0) < _need]
            if _missing:
                st.caption("Nog nodig: " + " · ".join(_missing))
        elif phase == "deploy":
            _ds = get_phase_stats(_cid, "deploy") or {}
            _done = _ds.get("triggered", 0) or 0
            _mx = lcfg["max_live_trades"]
            st.progress(min(1.0, _done / _mx) if _mx else 0.0,
                        text=f"📊 Live trades: {_done}/{_mx}")
            _pnl = _ds.get("total_pnl")
            if _pnl is not None:
                st.caption(f"P&L deze fase: €{_pnl:+.2f} (noodstop bij −€{lcfg['max_live_loss']:.0f})")
        elif phase == "validate":
            _vs = get_phase_stats(_cid, "validate") or {}
            _done = _vs.get("triggered", 0) or 0
            _mx = lcfg["min_validate_trades"]
            st.progress(min(1.0, _done / _mx) if _mx else 0.0,
                        text=f"📊 Validatie trades: {_done}/{_mx}")
    if phase in ("learn", "deploy", "validate"):
        st.caption("De fase eindigt zodra de eerste limiet wordt geraakt (tijd óf trades).")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Phase", f"{phase_emoji.get(phase, '⚪')} {phase.upper()}")
    col2.metric("Cycle #", cycle.get("cycle_number", "?"))
    _ps = cycle.get("phase_started_at") or ""
    started = _to_local(_ps).strftime("%d-%m %H:%M") if _ps else "—"
    col3.metric("Phase started", started)
    conf = cycle.get("confidence_score")
    col4.metric("Last confidence", f"{conf*100:.0f}%" if conf is not None else "—")

    # Phase stats
    cycle_id = cycle.get("id")
    if cycle_id:
        for ph, label in [("learn", "📚 Learn"), ("deploy", "🚀 Deploy"), ("validate", "🔍 Validate")]:
            stats = get_phase_stats(cycle_id, ph)
            if stats and stats.get("trades", 0) > 0:
                with st.expander(f"{label} phase stats ({stats.get('trades', 0)} trades)"):
                    cs = st.columns(4)
                    cs[0].metric("Trades", stats.get("trades", 0))
                    cs[1].metric("Triggered", stats.get("triggered", 0))
                    wr = stats.get("win_rate")
                    cs[2].metric("Win Rate", f"{wr:.1f}%" if wr is not None else "—")
                    pnl = stats.get("total_pnl")
                    cs[3].metric("Total P&L", f"€{pnl:+.2f}" if pnl is not None else "—")

    # Last manual Claude analysis from analytics tab
    manual = get_latest_manual_analysis()
    if manual and manual.get("claude_analysis"):
        with st.expander("🤖 Laatste handmatige Claude-analyse (analytics tab)"):
            st.markdown(manual["claude_analysis"][:3000])
            if manual.get("claude_params"):
                try:
                    params = json.loads(manual["claude_params"])
                    coin_p = params.get("coin_params", {})
                    if coin_p:
                        rows = []
                        for coin, cp in coin_p.items():
                            rows.append({
                                "Coin": coin,
                                "Trigger": cp.get("trigger_threshold", "—"),
                                "Cross": cp.get("cross_threshold", "—"),
                                "Offset": cp.get("initial_offset", "—"),
                                "Buffer": cp.get("ratchet_buffer", "—"),
                            })
                        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                except Exception:
                    pass

    # Claude's prediction — what Claude EXPECTED before deploy
    pred_raw = cycle.get("claude_prediction")
    if pred_raw:
        try:
            pred = json.loads(pred_raw)
            with st.expander("🔮 Claude's verwachting vóór deploy", expanded=(phase == "deploy")):
                pc1, pc2, pc3 = st.columns(3)
                pc1.metric("Verwachte winrate", f"{pred.get('expected_win_rate_pct', '?')}%")
                pc2.metric("Verwachte gem. P&L", f"€{pred.get('expected_avg_pnl_per_trade', 0):+.4f}")
                pc3.metric("Horizon (trades)", pred.get("prediction_horizon_trades", "?"))
                st.markdown(f"**Kernaan­name:** {pred.get('key_assumption', '—')}")
                st.markdown(f"**Falsificatie­conditie:** _{pred.get('falsifiable_condition', '—')}_")
                st.caption(
                    "Dit is Claude's expliciete verwachting VOOR deploy. "
                    "Als de deploy-resultaten de falsificatieconditie raken, "
                    "moet Claude zijn aanname herzien in de volgende analyse."
                )
                # Compare with actual deploy stats if available
                cycle_id = cycle.get("id")
                if cycle_id and phase in ("deploy", "validate", "analyze"):
                    deploy_stats = get_phase_stats(cycle_id, "deploy")
                    if deploy_stats and deploy_stats.get("trades", 0) > 0:
                        st.markdown("**Werkelijk (deploy fase tot nu):**")
                        dc1, dc2, dc3 = st.columns(3)
                        actual_wr = deploy_stats.get("win_rate")
                        actual_pnl = deploy_stats.get("avg_pnl")
                        expected_wr = pred.get("expected_win_rate_pct")
                        expected_pnl = pred.get("expected_avg_pnl_per_trade")
                        wr_delta = round(actual_wr - expected_wr, 1) if (actual_wr and expected_wr) else None
                        pnl_delta = round(actual_pnl - expected_pnl, 4) if (actual_pnl and expected_pnl) else None
                        dc1.metric("Werkelijke winrate", f"{actual_wr}%" if actual_wr else "—",
                                   delta=f"{wr_delta:+.1f}%" if wr_delta is not None else None)
                        dc2.metric("Werkelijke gem. P&L", f"€{actual_pnl:+.4f}" if actual_pnl else "—",
                                   delta=f"€{pnl_delta:+.4f}" if pnl_delta is not None else None)
                        dc3.metric("Deploy trades", deploy_stats.get("trades", 0))
        except Exception:
            pass

    # Claude's analysis (from learning cycle)
    if cycle.get("claude_analysis"):
        with st.expander("Claude's redenering"):
            st.markdown(cycle["claude_analysis"][:3000])

    # Active Claude params
    if cycle.get("claude_params"):
        try:
            params = json.loads(cycle["claude_params"])
            coin_p = params.get("coin_params", {})
            if coin_p:
                with st.expander("Active Claude parameters"):
                    rows = []
                    for coin, cp in coin_p.items():
                        rows.append({
                            "Coin": coin,
                            "Trigger": cp.get("trigger_threshold", "—"),
                            "Cross": cp.get("cross_threshold", "—"),
                            "Offset": cp.get("initial_offset", "—"),
                            "Buffer": cp.get("ratchet_buffer", "—"),
                            "Enabled": cp.get("enabled", True),
                        })
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        except Exception:
            pass

    # ── Pattern Matching ────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### 🎯 Patroonherkenning")
    st.caption(
        "Elke 30 minuten vergelijkt de bot de huidige marktomstandigheden "
        "(regime, conviction, OFI) met historische trades onder dezelfde condities. "
        "Dit geeft een verwacht win%-resultaat voor de lopende markten."
    )

    try:
        from src.pattern_matcher import get_last_results, get_last_ts, build_pattern_context_for_prompt
        pm_results = get_last_results()
        pm_ts = get_last_ts()
    except Exception:
        pm_results, pm_ts = {}, ""

    if pm_ts:
        try:
            _pm_age = (datetime.now(timezone.utc) - datetime.fromisoformat(pm_ts)).total_seconds() / 60
            st.caption(f"Laatste run: {_to_local(pm_ts).strftime('%d-%m %H:%M')} ({_pm_age:.0f} min geleden)")
        except Exception:
            st.caption(f"Laatste run: {pm_ts[:16]}")
    else:
        st.caption("Nog niet uitgevoerd (start bij eerste deploy).")

    col_pm_run, _ = st.columns([1, 3])
    with col_pm_run:
        if st.button("▶ Voer nu uit", key="run_pm_now"):
            write_command("run_pattern_match")
            with st.spinner("Patroonanalyse wordt uitgevoerd..."):
                import time as _time
                _time.sleep(3)
            st.rerun()

    if pm_results:
        pm_rows = []
        for coin, data in pm_results.items():
            fp = data.get("fingerprint", {})
            m  = data.get("match", {})
            win_pct = m.get("win_pct")
            avg_pnl = m.get("avg_pnl")
            pm_rows.append({
                "Coin":   f"{COIN_EMOJI.get(coin,'')} {coin}",
                "Regime": fp.get("regime", "?"),
                "Conviction": fp.get("conviction_bucket", "?"),
                "OFI":    fp.get("ofi_bucket", "?"),
                "Hist. win%": f"{win_pct:.1f}%" if win_pct is not None else "—",
                "Hist. P&L": f"€{avg_pnl:+.4f}" if avg_pnl is not None else "—",
                "Steekproef": m.get("n", "—"),
                "Match":  m.get("match_quality", "—"),
            })
        st.dataframe(pd.DataFrame(pm_rows), hide_index=True, use_container_width=True)

        # Visual alert for poor historical patterns
        bad_coins = [
            f"{COIN_EMOJI.get(c,'')} {c} ({(r.get('match',{}).get('win_pct') or 0):.1f}%)"
            for c, r in pm_results.items()
            if (r.get("match", {}).get("win_pct") or 0) < 52
            and (r.get("match", {}).get("n") or 0) >= 10
        ]
        if bad_coins:
            st.warning(
                f"⚠️ Historisch zwakke condities: {', '.join(bad_coins)}. "
                "Overweeg trigger_threshold tijdelijk te verhogen of coin te pauzeren."
            )
    else:
        st.caption("Geen patroondata beschikbaar.")

    # Control buttons
    st.markdown("---")
    st.markdown("**Handmatige bediening**")

    # Flow explanation per phase
    _next_phase_label = {
        "learn": "→ Analyse (Claude)",
        "analyze": "→ Live (Deploy)",
        "deploy": "→ Analyse (Claude)",
        "validate": "→ Learn (paper)",
    }
    _next_help = {
        "learn": "Slaat resterende paper-trades over en start Claude-analyse. Bij vertrouwen ≥60% gaat bot direct live.",
        "analyze": "Sla Claude-analyse over en ga direct live (gebruikt huidige config-params).",
        "deploy": "Stop live-trades vroeg en start meteen een Claude-heranalyse.",
        "validate": "Sla validate-fase over en start nieuwe learn-cyclus.",
    }
    next_label = _next_phase_label.get(phase, "Force next phase")
    next_help = _next_help.get(phase, "")

    # Automatic flow info
    if phase == "learn":
        st.info(
            "**Flow:** learn (paper) → **analyse** (Claude, ~2 min) → **live** (deploy, max "
            f"{CONFIG['learning']['max_live_trades']} trades / €{CONFIG['learning']['max_live_loss']:.0f} verlies) "
            "→ **analyse** → live → … Bij vertrouwen <60% valt het terug naar learn."
        )
    elif phase == "deploy":
        deploy_stats = get_phase_stats(cycle_id, "deploy") if cycle_id else {}
        live_done = deploy_stats.get("trades", 0) if deploy_stats else 0
        live_max = CONFIG["learning"]["max_live_trades"]
        st.success(
            f"**Live aan het traden.** {live_done}/{live_max} trades. "
            f"Na {live_max} trades (of -{CONFIG['learning']['max_live_loss']:.0f} EUR) heranalyseert Claude automatisch."
        )

    c1, c2, c3, c4 = st.columns(4)
    if c1.button(next_label, use_container_width=True, help=next_help):
        write_command("force_next_phase")
        st.toast(f"Faseovergang aangevraagd: {next_label}", icon="⏭")
    if c2.button("Reset cyclus", use_container_width=True, help="Sluit huidige cyclus af en start nieuw van learn."):
        write_command("reset_learning_cycle")
        st.toast("Cyclus reset aangevraagd.", icon="🔄")
    if is_killed():
        if c3.button("▶ Hervat", use_container_width=True, type="primary",
                     help="Herstart trading na pauze."):
            if KILL_FLAG_PATH.exists():
                KILL_FLAG_PATH.unlink()
            write_command("reset_kill")
            st.rerun()
    else:
        if c3.button("⏸ Pauzeer", use_container_width=True,
                     help="Kill-switch: stopt alle nieuwe trades."):
            write_command("kill")
            st.toast("Bot gepauzeerd.", icon="⏸")
    if phase in ("learn", "analyze") and c4.button("Ga direct live", use_container_width=True,
                                                    help="Ga direct naar live deploy, ongeacht huidige fase."):
        write_command("force_deploy")
        st.toast("Direct live aangevraagd.", icon="🚀")
    elif phase != "learn":
        c4.empty()


@st.fragment(run_every=10)
def _portfolio_live() -> None:
    pf = get_portfolio_snapshot()

    if pf["value"] is None:
        st.info("Portfolio data nog niet beschikbaar. Bot moet draaien om data op te halen (max 30s na start).")
        return

    updated = f" — bijgewerkt {fmt_time(pf['updated_at'])}" if pf["updated_at"] else ""
    st.markdown(f"### 💼 Polymarket Portfolio{updated}")

    usdc = pf["usdc"] or 0.0
    value = pf["value"] or 0.0
    start = pf["start_usdc"]
    pos_value = value - usdc

    c = st.columns(4)
    c[0].metric("USDC (vrij)", f"${usdc:.2f}")
    c[1].metric("Positiewaarde", f"${pos_value:.2f}")
    c[2].metric("Totaal portfolio", f"${value:.2f}")
    if start is not None:
        pnl = value - start
        c[3].metric("P&L vs start", f"${pnl:+.2f}", delta=f"{pnl/start*100:+.1f}%" if start else None)
    else:
        c[3].metric("Startkapitaal", "—")

    if st.button("🔄 Reset startkapitaal naar huidig", key="pf_reset_start"):
        write_command("reset_portfolio_start")
        st.toast("Startkapitaal bijgewerkt.", icon="🔄")

    st.divider()

    # ── Open posities ──────────────────────────────────────────────────────────
    positions = pf["positions"]
    st.markdown(f"### Open posities ({len(positions)})")

    if not positions:
        st.caption("Geen open posities op Polymarket.")
    else:
        rows = []
        for pos in positions:
            rows.append({
                "Markt": pos.get("title", pos.get("token_id", "")[:24]),
                "Uitkomst": pos.get("outcome", "—"),
                "Aandelen": pos.get("size", 0),
                "Avg prijs": f"${pos['avg_price']:.4f}" if pos.get("avg_price") is not None else "—",
                "Huidig": f"${pos['cur_price']:.4f}" if pos.get("cur_price") is not None else "—",
                "Waarde": f"${pos['value']:.2f}" if pos.get("value") is not None else "—",
                "P&L": f"${pos['pnl']:+.2f}" if pos.get("pnl") is not None else "—",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.divider()

    # ── Clear orphaned posities ────────────────────────────────────────────────
    st.markdown("### 🧹 Wees Posities")
    st.caption(
        "Verkoopt alle Polymarket-posities die **niet** bij een actieve bot-trade horen. "
        "Actieve trades worden afgemaakt. Posities die via held\\_for\\_resolution worden gehouden "
        "worden WEL verkocht — check dit eerst."
    )
    col_cb, col_btn = st.columns([3, 1])
    confirmed = col_cb.checkbox("Ik begrijp dit — verwijder wees-posities", key="pf_clear_confirm")
    if col_btn.button("🧹 Clear", disabled=not confirmed, type="primary", key="pf_clear_btn"):
        write_command("clear_orphaned_positions")
        st.toast("Opdracht verstuurd — bot verkoopt wees-posities.", icon="🧹")
        st.rerun()


def _portfolio_panel() -> None:
    _portfolio_live()


# ── Signal Trader Panel ────────────────────────────────────────────────────────

_ST_CONFIG_PATH = Path(__file__).parent / "config" / "config.yaml"


def _read_st_cfg_from_yaml() -> dict:
    """Lees signal_trader config direct uit config.yaml (bypassed module-cache).

    CONFIG is een module-level singleton die niet herlaadt na een YAML-schrijf.
    Deze functie leest elke keer vers uit het bestand zodat instellingen na
    opslaan correct worden weergegeven.
    """
    try:
        import yaml
        with open(_ST_CONFIG_PATH) as f:
            data = yaml.safe_load(f)
        return (data or {}).get("signal_trader", {}) or {}
    except Exception:
        return CONFIG.get("signal_trader", {})


def _save_signal_trader_to_yaml(global_params: dict, coin_params: dict) -> str:
    """Schrijf signal_trader config naar config.yaml. Geeft '' bij succes, fout-string bij fout."""
    try:
        from ruamel.yaml import YAML
        ryaml = YAML()
        ryaml.preserve_quotes = True
        ryaml.width = 4096
        with open(_ST_CONFIG_PATH) as f:
            cfg = ryaml.load(f)
        if "signal_trader" not in cfg or cfg["signal_trader"] is None:
            cfg["signal_trader"] = {}
        for k, v in global_params.items():
            cfg["signal_trader"][k] = v
        if "coins" not in cfg["signal_trader"] or cfg["signal_trader"]["coins"] is None:
            cfg["signal_trader"]["coins"] = {}
        for coin, coin_cfg in coin_params.items():
            if coin not in cfg["signal_trader"]["coins"] or cfg["signal_trader"]["coins"][coin] is None:
                cfg["signal_trader"]["coins"][coin] = {}
            for k, v in coin_cfg.items():
                cfg["signal_trader"]["coins"][coin][k] = v
        with open(_ST_CONFIG_PATH, "w") as f:
            ryaml.dump(cfg, f)
        return ""
    except ImportError:
        pass
    except Exception as exc:
        return str(exc)
    # Fallback: PyYAML
    try:
        import yaml
        with open(_ST_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        cfg.setdefault("signal_trader", {}).update(global_params)
        coins_section = cfg["signal_trader"].setdefault("coins", {})
        for coin, coin_cfg in coin_params.items():
            coins_section.setdefault(coin, {}).update(coin_cfg)
        with open(_ST_CONFIG_PATH, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return ""
    except Exception as exc2:
        return str(exc2)


def _read_router_cfg_from_yaml() -> dict:
    """Lees router config direct uit config.yaml (bypassed module-cache)."""
    try:
        import yaml
        with open(_ST_CONFIG_PATH) as f:
            data = yaml.safe_load(f)
        return (data or {}).get("router", {}) or {}
    except Exception:
        return CONFIG.get("router", {})


def _save_router_to_yaml(params: dict) -> str:
    """Schrijf router config naar config.yaml. Geeft '' bij succes, fout-string bij fout."""
    try:
        from ruamel.yaml import YAML
        ryaml = YAML()
        ryaml.preserve_quotes = True
        ryaml.width = 4096
        with open(_ST_CONFIG_PATH) as f:
            cfg = ryaml.load(f)
        if "router" not in cfg or cfg["router"] is None:
            cfg["router"] = {}
        for k, v in params.items():
            cfg["router"][k] = v
        with open(_ST_CONFIG_PATH, "w") as f:
            ryaml.dump(cfg, f)
        return ""
    except Exception as exc:
        return str(exc)


def _save_multi_section_to_yaml(sections: dict) -> str:
    """Schrijf meerdere config-secties tegelijk naar config.yaml.

    sections: {"router": {"key": val, ...}, "trading": {"key": val}, ...}
    """
    try:
        from ruamel.yaml import YAML
        ryaml = YAML()
        ryaml.preserve_quotes = True
        ryaml.width = 4096
        with open(_ST_CONFIG_PATH) as f:
            cfg = ryaml.load(f)
        for section, params in sections.items():
            if section not in cfg or cfg[section] is None:
                cfg[section] = {}
            for k, v in params.items():
                cfg[section][k] = v
        with open(_ST_CONFIG_PATH, "w") as f:
            ryaml.dump(cfg, f)
        return ""
    except Exception as exc:
        return str(exc)


@st.fragment(run_every=15)
def _oracle_temperature_widget() -> None:
    """Handels Temperatuur widget: composiet 0–100 gauge met kleur en context."""
    st.markdown("#### 🌡️ Handels Temperatuur")

    # Read cached value from dashboard_state (updated every 60s by oracle background task)
    temp_raw = get_state("oracle_trading_temperature")
    fg_val   = get_state("oracle_fear_greed_value")
    fg_label = get_state("oracle_fear_greed_label") or "Onbekend"
    news_s   = get_state("oracle_news_sentiment") or "onbekend"
    pm_dir   = get_state("oracle_polymarket_dir") or "NEUTRAL"
    track_r  = get_state("oracle_track_record")
    pat_win  = get_state("oracle_pattern_win_prob")

    try:
        temp = int(float(temp_raw)) if temp_raw else None
    except (ValueError, TypeError):
        temp = None

    if temp is None:
        st.caption("Temperatuur nog niet beschikbaar — Oracle draait of nog niet gestart.")
        oracle_enabled = CONFIG.get("oracle", {}).get("enabled", False)
        if not oracle_enabled:
            st.info("Oracle is uitgeschakeld (`oracle.enabled: false` in config.yaml). "
                    "Zet `enabled: true` en herstart de bot om temperatuurdata te zien.")
        return

    # Colour + label based on temperature
    if temp < 30:
        color, label = "#3b82f6", "🔵 IJskoud"
    elif temp < 50:
        color, label = "#eab308", "🟡 Koud"
    elif temp < 70:
        color, label = "#f97316", "🟠 Lauw"
    elif temp < 85:
        color, label = "#22c55e", "🟢 Warm"
    else:
        color, label = "#10b981", "✅ Heet"

    # Big gauge bar
    bar_pct = temp
    st.markdown(
        f'<div style="margin-bottom:4px">'
        f'<span style="font-size:2rem;font-weight:bold;color:{color}">{temp}/100</span>'
        f'&nbsp;&nbsp;<span style="color:{color};font-size:1rem">{label}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.progress(bar_pct / 100)

    # Context row
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        fg_str = f"{int(float(fg_val))}" if fg_val else "—"
        st.metric("Fear & Greed", f"{fg_str} ({fg_label})" if fg_val else "—")
    with c2:
        st.metric("Nieuws", news_s.capitalize())
    with c3:
        st.metric("Polymarket dir.", pm_dir)
    with c4:
        tr_str = f"{float(track_r)*100:.0f}%" if track_r else "—"
        st.metric("Track record", tr_str)

    oracle_cfg = CONFIG.get("oracle", {})
    gate_on  = oracle_cfg.get("hard_gate", False)
    hard_thr = oracle_cfg.get("temperature_hard_block", 30)
    soft_thr = oracle_cfg.get("temperature_soft_block", 50)
    coin_paper_thr = int(oracle_cfg.get("per_coin_temp_paper_threshold", 35))
    if not gate_on:
        st.caption("ℹ️ Hard gate uitgeschakeld (`oracle.hard_gate: false`) — Orakel logt alleen, blokkeert niet.")
    elif temp < hard_thr:
        st.warning(f"🚫 Hard gate ACTIEF — temperatuur {temp} < {hard_thr}. Trades geblokkeerd.")
    elif temp < soft_thr:
        st.warning(f"⚠️ Zachte drempel — temperatuur {temp} < {soft_thr}. Discord confirm vereist (indien ingeschakeld).")
    else:
        st.success("✅ Orakel keurt trades goed.")

    # Per-coin temperature overview
    coins = list(CONFIG.get("coins", {}).keys())
    coin_temps = []
    for c in coins:
        t_raw = get_state(f"oracle_temp_{c}")
        try:
            ct = int(float(t_raw)) if t_raw else None
        except (ValueError, TypeError):
            ct = None
        forced_paper = ct is not None and oracle_cfg.get("enabled", False) and ct < coin_paper_thr
        coin_temps.append({"coin": c, "temp": ct, "paper_forced": forced_paper})

    if any(x["temp"] is not None for x in coin_temps):
        st.markdown("**Per-coin temperatuur:**")
        cols = st.columns(len(coin_temps))
        for i, ct_row in enumerate(coin_temps):
            c_name = ct_row["coin"]
            c_temp = ct_row["temp"]
            with cols[i]:
                if c_temp is None:
                    st.metric(c_name, "—")
                else:
                    c_color = "normal" if not ct_row["paper_forced"] else "inverse"
                    label = f"{c_temp}/100"
                    delta = "📄 paper" if ct_row["paper_forced"] else None
                    st.metric(c_name, label, delta=delta,
                              delta_color="off" if delta else "normal")


def _oracle_verdicts_panel() -> None:
    """Recente Oracle verdicts — per-trade approve/reject beslissingen."""
    import json as _json

    st.markdown("#### 🔮 Oracle Verdicts")
    verdicts = get_oracle_verdicts(limit=30)
    if not verdicts:
        oracle_enabled = CONFIG.get("oracle", {}).get("enabled", False)
        if oracle_enabled:
            st.caption("Nog geen verdicts — Oracle logt zodra de eerste trade langs de gate gaat.")
        else:
            st.caption("Oracle uitgeschakeld.")
        return

    hard_gate = CONFIG.get("oracle", {}).get("hard_gate", False)
    if not hard_gate:
        st.caption("ℹ️ `hard_gate: false` — Orakel logt verdicts maar blokkeert geen trades.")

    for v in verdicts[:15]:
        approved = bool(v.get("approved"))
        reason   = v.get("reason", "—")
        temp     = v.get("trading_temperature")
        coin     = v.get("coin", "—")
        ts       = (v.get("created_at") or "")[:16]
        correct  = v.get("correct")

        # Macro context
        try:
            ctx = _json.loads(v.get("macro_context") or "{}")
        except Exception:
            ctx = {}

        fg    = ctx.get("fear_greed", "—")
        news  = ctx.get("news_sentiment", "—")
        conf  = ctx.get("conviction", "—")

        if approved:
            icon, bg = "✅", "rgba(16,185,129,0.1)"
            border = "#10b981"
        else:
            icon, bg = "🚫", "rgba(239,68,68,0.1)"
            border = "#ef4444"

        if correct is True:
            outcome_badge = '<span style="color:#10b981;font-size:0.75rem">✓ correct</span>'
        elif correct is False:
            outcome_badge = '<span style="color:#ef4444;font-size:0.75rem">✗ incorrect</span>'
        else:
            outcome_badge = '<span style="color:#6b7280;font-size:0.75rem">⏳ lopend</span>'

        temp_str = f"{temp}/100" if temp is not None else "—"
        reason_clean = reason.replace("_", " ")

        st.markdown(
            f'<div style="background:{bg};border:1px solid {border};border-radius:6px;'
            f'padding:8px 12px;margin-bottom:6px;font-size:0.875rem">'
            f'{icon} <b>{coin}</b> &nbsp;·&nbsp; 🌡️ {temp_str} &nbsp;·&nbsp; {reason_clean}'
            f'&nbsp;&nbsp;<span style="color:#6b7280">{ts}</span>'
            f'&nbsp;&nbsp;{outcome_badge}'
            f'<br><span style="color:#9ca3af;font-size:0.75rem">'
            f'F&G: {fg} &nbsp;·&nbsp; Nieuws: {news} &nbsp;·&nbsp; Conviction: {conf}'
            f'</span></div>',
            unsafe_allow_html=True,
        )

    # Track record summary
    total = len(verdicts)
    with_outcome = [v for v in verdicts if v.get("correct") is not None]
    if with_outcome:
        n_correct = sum(1 for v in with_outcome if v.get("correct"))
        acc = n_correct / len(with_outcome) * 100
        st.caption(
            f"Track record: **{acc:.0f}%** correct over {len(with_outcome)} afgeronde verdicts "
            f"(van {total} totaal)"
        )


def _oracle_pattern_table() -> None:
    """Patroon-statistieken uit de DB: regime × conviction × bucket → win%."""
    st.markdown("#### 📊 Handelspatronen uit DB")

    coins = list(CONFIG.get("coins", {}).keys())
    coin_options = ["Alle coins"] + coins
    sel = st.selectbox("Coin filter", coin_options, key="oracle_pattern_coin", index=0)
    coin_filter = None if sel == "Alle coins" else sel

    days_sel = st.selectbox("Periode", [7, 14, 30, 60], index=2,
                             format_func=lambda d: f"Afgelopen {d} dagen",
                             key="oracle_pattern_days")

    tab_regime, tab_signal = st.tabs(["Regime × Conviction", "OFI × Funding × Regime"])

    with tab_regime:
        rows = get_oracle_pattern_stats(coin=coin_filter, days=days_sel)
        if not rows:
            st.caption("Nog niet genoeg data (minimaal 5 trades per combinatie).")
        else:
            df = pd.DataFrame(rows)
            # Colour-code win_pct column
            def _color_win(val):
                if val >= 60:
                    return "background-color:#16a34a33;color:#16a34a"
                if val <= 40:
                    return "background-color:#dc262633;color:#dc2626"
                return ""
            st.dataframe(
                df.rename(columns={
                    "regime": "Regime", "conv_bucket": "Conviction",
                    "bucket": "Bucket", "n": "n",
                    "win_pct": "Win %", "avg_pnl": "Gem P&L", "total_pnl": "Totaal P&L",
                }).style.applymap(_color_win, subset=["Win %"]),
                hide_index=True,
                use_container_width=True,
            )
            st.caption(f"{len(rows)} combinaties getoond (min 5 trades)")

    with tab_signal:
        rows2 = get_oracle_signal_patterns(coin=coin_filter, days=days_sel)
        if not rows2:
            st.caption("Nog niet genoeg data (minimaal 5 trades per combinatie).")
        else:
            df2 = pd.DataFrame(rows2)
            st.dataframe(
                df2.rename(columns={
                    "ofi_bucket": "OFI", "fr_bucket": "Funding rate",
                    "regime": "Regime", "n": "n",
                    "win_pct": "Win %", "avg_pnl": "Gem P&L",
                }),
                hide_index=True,
                use_container_width=True,
            )
            st.caption(f"{len(rows2)} combinaties getoond (min 5 trades)")


@st.fragment
def _auto_router_panel() -> None:
    """Dashboard panel voor de auto_router modus — alle instellingen op één plek."""
    mode      = current_mode()
    cfg       = _read_router_cfg_from_yaml()
    st_cfg    = _read_st_cfg_from_yaml()
    is_active = (mode == "auto_router")
    straddle_paper = cfg.get("paper_mode", True)
    signal_paper   = st_cfg.get("paper_mode", True)

    saved_straddle_size = get_state("trade_size_eur")
    straddle_size_db = float(saved_straddle_size) if saved_straddle_size else CONFIG["trading"].get("trade_size_eur", 1.0)

    # ── Status banner ────────────────────────────────────────────────────────
    if is_active:
        straddle_label = "📄 Paper" if straddle_paper else "💸 Live"
        signal_label   = "📄 Paper" if signal_paper   else "💸 Live"
        st.markdown(
            f'<div style="background:rgba(139,92,246,0.15);border:1px solid #8b5cf6;'
            f'border-radius:8px;padding:10px 16px;margin-bottom:12px">'
            f'🤖 <b>Auto Router is ACTIEF</b> &nbsp;·&nbsp; '
            f'Straddle: {straddle_label} &nbsp;|&nbsp; Signal: {signal_label}'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div style="background:rgba(55,65,81,0.2);border:1px solid #374151;'
            'border-radius:8px;padding:10px 16px;margin-bottom:12px">'
            '⏸ Auto Router is <b>inactief</b> — activeer via de knop hieronder'
            '</div>',
            unsafe_allow_html=True,
        )

    # ── Activeer / Deactiveer ────────────────────────────────────────────────
    col_act, col_deact = st.columns(2)
    with col_act:
        if st.button("▶ Activeer", type="primary", disabled=is_active, key="ar_activate"):
            write_command("set_mode", {"mode": "auto_router"})
            st.rerun()
    with col_deact:
        if st.button("⏹ Deactiveer", disabled=not is_active, key="ar_deactivate"):
            write_command("set_mode", {"mode": "paper_auto"})
            st.rerun()

    st.divider()

    # ── Configuratie form ────────────────────────────────────────────────────
    with st.form("ar_config_form"):
        st.markdown("#### Trade groottes")
        c1, c2 = st.columns(2)
        with c1:
            straddle_size = st.number_input(
                "Straddle inleg per trade (EUR)",
                min_value=0.50, max_value=500.0,
                value=straddle_size_db, step=0.50, format="%.2f",
                help="trading.trade_size_eur — gebruikt door straddle_asym en straddle_sym bucket",
                key="ar_straddle_size",
            )
        with c2:
            signal_size = st.number_input(
                "Signal inleg per trade (EUR)",
                min_value=0.50, max_value=500.0,
                value=float(st_cfg.get("trade_size_eur", 10.0)),
                step=0.50, format="%.2f",
                help="signal_trader.trade_size_eur — gebruikt door signal bucket",
                key="ar_signal_size",
            )

        st.markdown("#### Paper / Live per bucket")
        c3, c4 = st.columns(2)
        with c3:
            new_straddle_paper = st.selectbox(
                "Straddle bucket",
                ["📄 Paper (simulatie)", "💸 Live (echte orders)"],
                index=0 if straddle_paper else 1,
                key="ar_straddle_mode",
                help="Paper = geen echte USDC uitgaven. Live = echte orders.",
            )
        with c4:
            new_signal_paper = st.selectbox(
                "Signal bucket",
                ["📄 Paper (simulatie)", "💸 Live (echte orders)"],
                index=0 if signal_paper else 1,
                key="ar_signal_mode",
            )

        st.markdown("#### Take it / Save it")
        c5, c6 = st.columns(2)
        with c5:
            take_it_enabled = st.toggle(
                "Take it ingeschakeld",
                value=cfg.get("take_it_enabled", True),
                key="ar_take_it_enabled",
                help="Koop extra winner-tokens als de trade duidelijk wint (mid ≥ 0.82, ≤ 240s resterend)",
            )
            take_it_size = st.number_input(
                "Take it extra inleg (EUR)",
                min_value=0.50, max_value=50.0,
                value=float(cfg.get("take_it_size_eur", 3.0)),
                step=0.50, format="%.2f",
                disabled=not take_it_enabled,
                key="ar_take_it_size",
            )
        with c6:
            save_it_enabled = st.toggle(
                "Save it ingeschakeld",
                value=cfg.get("save_it_enabled", True),
                key="ar_save_it_enabled",
                help="Koop de keerzijde als de markt omdraait (winner daalt ≥ 0.20 van piek, ≥ 120s resterend)",
            )
            save_it_size = st.number_input(
                "Save it extra inleg (EUR)",
                min_value=0.50, max_value=50.0,
                value=float(cfg.get("save_it_size_eur", 3.0)),
                step=0.50, format="%.2f",
                disabled=not save_it_enabled,
                key="ar_save_it_size",
            )

        st.markdown("#### Routing drempels")
        c7, c8, c9 = st.columns(3)
        with c7:
            sig_conv = st.slider(
                "Signal min. conviction",
                min_value=0.30, max_value=0.90, step=0.05,
                value=float(cfg.get("signal_min_conviction", 0.55)),
                key="ar_sig_conv",
                help="Trades met conviction ≥ deze waarde gaan naar signal bucket",
            )
        with c8:
            str_conv = st.slider(
                "Straddle min. conviction",
                min_value=0.30, max_value=0.95, step=0.05,
                value=float(cfg.get("straddle_min_conviction", 0.70)),
                key="ar_str_conv",
                help="Trades met conviction ≥ deze waarde gaan naar straddle bucket (als signal uitstaat)",
            )
        with c9:
            max_corr = st.number_input(
                "Max gecorr. posities",
                min_value=1, max_value=10,
                value=int(cfg.get("max_correlated_positions", 3)),
                step=1,
                key="ar_max_corr",
                help="Blokkeer nieuwe trade als ≥ N open posities al dezelfde richting hebben",
            )

        submitted = st.form_submit_button("💾 Opslaan", type="primary")

    if submitted:
        new_straddle_paper_bool = "Paper" in new_straddle_paper
        new_signal_paper_bool   = "Paper" in new_signal_paper

        router_params = {
            "paper_mode":               new_straddle_paper_bool,
            "take_it_enabled":          take_it_enabled,
            "take_it_size_eur":         take_it_size,
            "save_it_enabled":          save_it_enabled,
            "save_it_size_eur":         save_it_size,
            "signal_min_conviction":    round(sig_conv, 2),
            "straddle_min_conviction":  round(str_conv, 2),
            "max_correlated_positions": max_corr,
        }
        err = _save_multi_section_to_yaml({
            "router":       router_params,
            "trading":      {"trade_size_eur": straddle_size},
            "signal_trader": {"trade_size_eur": signal_size, "paper_mode": new_signal_paper_bool},
        })
        if err:
            st.error(f"YAML opslaan mislukt: {err}")
        else:
            write_command("apply_router_config", router_params)
            write_command("set_trade_size", {"trade_size_eur": straddle_size})
            write_command("apply_signal_trader_config", {"trade_size_eur": signal_size})
            write_command("set_signal_trader_paper", {"paper_mode": new_signal_paper_bool})
            st.success("Instellingen opgeslagen en doorgevoerd.")
            st.rerun()

    st.divider()
    _oracle_temperature_widget()
    st.divider()
    _oracle_verdicts_panel()
    st.divider()
    _oracle_pattern_table()
    st.divider()
    _router_trade_cards()


_BUCKET_META = {
    "signal":        {"emoji": "🎯", "label": "Signal",        "color": "#10b981"},
    "straddle_asym": {"emoji": "↕️",  "label": "Straddle asym", "color": "#3b82f6"},
    "straddle_sym":  {"emoji": "⚖️",  "label": "Straddle sym",  "color": "#8b5cf6"},
}


def _router_trade_cards() -> None:
    """Kaartjes met alle auto_router trades van de afgelopen 24 uur."""
    st.markdown("#### Trades (afgelopen 24u)")
    trades = get_router_trades(hours=24, limit=60)
    if not trades:
        st.caption("Nog geen trades gerouteerd in de afgelopen 24 uur.")
        return

    open_statuses = {"pending", "waiting", "entry_placed", "monitoring", "signal_holding"}

    for t in trades:
        trade_id   = t.get("trade_id", "")
        coin       = t.get("coin", "")
        bucket     = t.get("router_bucket") or t.get("triggered_by") or "?"
        conviction = t.get("router_conviction_score")
        regime     = t.get("regime_at_entry") or "—"
        status     = t.get("status", "")
        net_pnl    = t.get("net_pnl")
        winner_side = t.get("winner_side")
        exit_reason = t.get("winner_exit_reason") or ""
        created_at  = t.get("created_at", "")[:16]

        meta = _BUCKET_META.get(bucket, {"emoji": "🔀", "label": bucket, "color": "#6b7280"})
        is_open = status in open_statuses

        # P&L badge
        if net_pnl is not None:
            pnl_str   = f"{'+' if net_pnl >= 0 else ''}€{net_pnl:.2f}"
            pnl_color = "#10b981" if net_pnl >= 0 else "#ef4444"
        else:
            pnl_str   = "open" if is_open else "—"
            pnl_color = "#f59e0b" if is_open else "#6b7280"

        with st.container(border=True):
            col_icon, col_main, col_pnl, col_btn = st.columns([0.5, 4, 1.5, 0.8])

            with col_icon:
                st.markdown(
                    f'<div style="font-size:1.6rem;line-height:1;padding-top:4px">'
                    f'{meta["emoji"]}</div>',
                    unsafe_allow_html=True,
                )

            with col_main:
                st.markdown(
                    f'<b style="font-size:1rem">{COIN_EMOJI.get(coin,"")} {coin}</b>'
                    f' &nbsp;<span style="background:{meta["color"]}22;color:{meta["color"]};'
                    f'border-radius:4px;padding:1px 6px;font-size:0.75rem">'
                    f'{meta["label"]}</span>',
                    unsafe_allow_html=True,
                )
                detail_parts = []
                if conviction is not None:
                    detail_parts.append(f"conviction {conviction:.2f}")
                detail_parts.append(f"regime {regime}")
                if winner_side:
                    detail_parts.append(f"winner {winner_side}")
                if exit_reason:
                    detail_parts.append(exit_reason.replace("_", " "))
                st.caption(f"{created_at}  ·  {status}  ·  " + "  ·  ".join(detail_parts))

            with col_pnl:
                st.markdown(
                    f'<div style="color:{pnl_color};font-weight:bold;'
                    f'font-size:1rem;text-align:right;padding-top:6px">'
                    f'{pnl_str}</div>',
                    unsafe_allow_html=True,
                )

            with col_btn:
                if st.button("ℹ️", key=f"ar_card_{trade_id}", help="Details bekijken"):
                    st.session_state["_detail_trade_id"] = trade_id
                    st.session_state["_detail_allow_closed"] = True
                    st.rerun(scope="app")


@st.fragment(run_every=15)
def _signal_trader_live() -> None:
    """Dashboard panel voor de signal_trader modus."""
    mode      = current_mode()
    cfg       = _read_st_cfg_from_yaml()   # altijd vers uit YAML, niet de cache
    is_active = (mode == "signal_trader")
    is_paper  = cfg.get("paper_mode", True)

    # ── Status banner ───────────────────────────────────────────────────────────
    if is_active:
        paper_label = "📄 PAPER" if is_paper else "💸 LIVE"
        st.markdown(
            f'<div style="background:rgba(16,185,129,0.15);border:1px solid #34d399;'
            f'border-radius:8px;padding:10px 16px;margin-bottom:12px">'
            f'🎯 <b>Signal Trader is ACTIEF</b> &nbsp;·&nbsp; {paper_label} mode'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div style="background:rgba(55,65,81,0.2);border:1px solid #374151;'
            'border-radius:8px;padding:10px 16px;margin-bottom:12px">'
            '⏸ Signal Trader is <b>inactief</b> — activeer via de onderstaande knop'
            '</div>',
            unsafe_allow_html=True,
        )

    # ── Mode-knoppen ────────────────────────────────────────────────────────────
    col_act, col_deact, col_paper, col_live = st.columns(4)
    with col_act:
        if st.button("▶ Activeer", type="primary", disabled=is_active, key="st_activate"):
            write_command("set_mode", {"mode": "signal_trader"})
            st.rerun()
    with col_deact:
        if st.button("⏹ Deactiveer", disabled=not is_active, key="st_deactivate"):
            write_command("set_mode", {"mode": "paper_auto"})
            st.rerun()
    with col_paper:
        if st.button("📄 Paper", disabled=is_paper, key="st_paper"):
            _save_signal_trader_to_yaml({"paper_mode": True}, {})
            write_command("set_signal_trader_paper", {"paper_mode": True})
            st.rerun()
    with col_live:
        if st.button("💸 Live", disabled=not is_paper, type="secondary", key="st_live"):
            _save_signal_trader_to_yaml({"paper_mode": False}, {})
            write_command("set_signal_trader_paper", {"paper_mode": False})
            st.rerun()

    st.divider()

    # ── Periode + metrics ────────────────────────────────────────────────────────
    # (label → hours; None = alle tijd)
    _PERIOD_HOURS: dict[str, int | None] = {
        "1u":       1,
        "2u":       2,
        "4u":       4,
        "24u":      24,
        "2 dagen":  48,
        "7 dagen":  168,
        "Alle tijd": None,
    }
    period = st.radio(
        "Periode",
        list(_PERIOD_HOURS.keys()),
        index=0,                      # standaard: 1 uur
        horizontal=True,
        key="st_period",
    )
    _hours = _PERIOD_HOURS[period]

    trades = get_signal_trades(hours=_hours)
    df = pd.DataFrame(trades) if trades else pd.DataFrame()

    if not df.empty:
        won     = (df["winner_exit_reason"] == "resolution_won").sum() if "winner_exit_reason" in df.columns else 0
        lost    = (df["winner_exit_reason"] == "resolution_lost").sum() if "winner_exit_reason" in df.columns else 0
        pending = (df["status"] == "signal_holding").sum() if "status" in df.columns else 0
        closed  = won + lost
        accuracy = won / closed * 100.0 if closed else 0.0
        net_pnl  = df["net_pnl"].fillna(0).sum() if "net_pnl" in df.columns else 0.0
        break_even_pct = 52.0

        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Gesloten", closed)
        c2.metric("✅ Won", won)
        c3.metric("❌ Lost", lost)
        c4.metric("⏳ Open", pending)
        c5.metric("Accuracy", f"{accuracy:.1f}%" if closed else "—")
        c6.metric("Netto P&L", f"€{net_pnl:+.2f}")

        if closed >= 5:
            margin = accuracy - break_even_pct
            if accuracy >= break_even_pct:
                st.success(f"📈 **Boven break-even** ({break_even_pct:.0f}%) — winstgevend! Marge: **+{margin:.1f}pp**")
            else:
                st.error(f"📉 **Onder break-even** ({break_even_pct:.0f}%) — verliesgevend. Tekort: {margin:.1f}pp")
        else:
            st.caption(f"⏳ Min. 5 trades nodig voor break-even oordeel (nu {closed}).")
    else:
        st.info("Geen signal_trader trades gevonden voor deze periode.")

    st.divider()

    # ── Actieve posities (tegels) ────────────────────────────────────────────────
    active_trades = get_signal_active_trades()
    if active_trades:
        st.markdown(f"#### ⚡ Open posities ({len(active_trades)})")
        now_utc = datetime.now(timezone.utc)
        cols_per_row = 3
        rows_needed = (len(active_trades) + cols_per_row - 1) // cols_per_row
        for row_i in range(rows_needed):
            tile_cols = st.columns(cols_per_row)
            for col_i in range(cols_per_row):
                idx = row_i * cols_per_row + col_i
                if idx >= len(active_trades):
                    break
                t = active_trades[idx]
                with tile_cols[col_i]:
                    coin      = t.get("coin", "?")
                    side      = t.get("winner_side", "?")
                    conv      = t.get("conviction_score_at_entry")
                    direction = t.get("conviction_at_entry", "?")
                    fill_p    = t.get("entry_yes_price") or t.get("entry_no_price")
                    size      = t.get("entry_size")
                    paper     = "📄" if "paper" in str(t.get("triggered_by","")) else "💸"
                    q         = t.get("question", "")

                    # Resterende tijd
                    we_ts = t.get("window_end_ts")
                    if we_ts:
                        try:
                            we = datetime.fromisoformat(str(we_ts))
                            if we.tzinfo is None:
                                we = we.replace(tzinfo=timezone.utc)
                            secs_left = max(0, (we - now_utc).total_seconds())
                            mins_left = int(secs_left // 60)
                            secs_rem  = int(secs_left % 60)
                            time_str  = f"{mins_left}m {secs_rem:02d}s" if secs_left > 0 else "afgelopen"
                        except Exception:
                            time_str = "?"
                    else:
                        time_str = "?"

                    side_color = "#34d399" if side == "YES" else "#f87171"
                    dir_arrow  = "↑" if direction == "UP" else "↓"
                    emoji      = COIN_EMOJI.get(coin, "🔵")
                    conv_str   = f"{conv:.2f}" if conv else "—"
                    fill_str   = f"€{fill_p:.3f}" if fill_p else "—"
                    size_str   = f"{size:.1f} shares" if size else "—"
                    q_short    = (q[:55] + "…") if q and len(q) > 55 else (q or "—")

                    st.markdown(
                        f"""<div style="background:#111827;border:1px solid #1f2937;
                            border-left:4px solid {side_color};border-radius:10px;
                            padding:12px 14px;margin-bottom:8px">
                          <div style="font-size:16px;font-weight:700;margin-bottom:4px">
                            {emoji} {coin} &nbsp;
                            <span style="color:{side_color}">{side} {dir_arrow}</span>
                            &nbsp;<span style="font-size:12px;color:#6b7280">{paper}</span>
                          </div>
                          <div style="font-size:12px;color:#9ca3af;margin-bottom:2px">{q_short}</div>
                          <div style="display:flex;gap:16px;margin-top:6px;font-size:13px">
                            <span>🎯 Conv. <b>{conv_str}</b></span>
                            <span>💰 Entry <b>{fill_str}</b></span>
                            <span>📦 <b>{size_str}</b></span>
                          </div>
                          <div style="margin-top:6px;font-size:12px;color:#fbbf24">
                            ⏱ {time_str} resterend
                          </div>
                        </div>""",
                        unsafe_allow_html=True,
                    )
        st.divider()

    # ── Voltooide trades — pagineerd tabel ──────────────────────────────────────
    PER_PAGE   = 20
    MAX_PAGES  = 5

    if "st_trade_page" not in st.session_state:
        st.session_state["st_trade_page"] = 0

    page = st.session_state["st_trade_page"]
    completed, total_count = get_signal_trades_paginated(
        hours=_hours, page=page, per_page=PER_PAGE
    )
    max_page = min(MAX_PAGES - 1, max(0, (total_count - 1) // PER_PAGE))

    # Clamp page als filter veranderd
    if page > max_page:
        page = max_page
        st.session_state["st_trade_page"] = page

    shown_from = page * PER_PAGE + 1
    shown_to   = min((page + 1) * PER_PAGE, total_count)
    st.markdown(
        f"#### 📋 Voltooide trades"
        + (f" — {shown_from}–{shown_to} van {total_count}" if total_count else "")
    )

    if completed:
        cdf = pd.DataFrame(completed)

        def _fmt_result(row) -> str:
            reason = row.get("winner_exit_reason", "")
            status = row.get("status", "")
            if reason == "resolution_won":  return "✅ Won"
            if reason == "resolution_lost": return "❌ Lost"
            if "abort" in str(reason).lower() or status == "aborted": return "⚠️ Afgebr."
            return "—"

        show_cols = ["coin", "triggered_by", "winner_side",
                     "winner_exit_reason", "status",
                     "net_pnl", "conviction_score_at_entry",
                     "question", "created_at"]
        avail = [c for c in show_cols if c in cdf.columns]
        rec = cdf[avail].copy()

        if "triggered_by" in rec.columns:
            rec["Type"] = rec["triggered_by"].map(
                {"signal_paper": "📄", "signal_live": "💸", "signal": "📄"}
            ).fillna("—")
            rec = rec.drop(columns=["triggered_by"])
        if "winner_exit_reason" in rec.columns or "status" in rec.columns:
            rec["Resultaat"] = rec.apply(lambda r: _fmt_result(r.to_dict()), axis=1)
        if "winner_exit_reason" in rec.columns:
            rec = rec.drop(columns=["winner_exit_reason"])
        if "status" in rec.columns:
            rec = rec.drop(columns=["status"])
        if "net_pnl" in rec.columns:
            rec["P&L"] = rec["net_pnl"].apply(
                lambda x: f"€{x:+.2f}" if pd.notna(x) else "—")
            rec = rec.drop(columns=["net_pnl"])
        if "conviction_score_at_entry" in rec.columns:
            rec["Conv."] = rec["conviction_score_at_entry"].apply(
                lambda x: f"{x:.2f}" if pd.notna(x) else "—")
            rec = rec.drop(columns=["conviction_score_at_entry"])
        if "question" in rec.columns:
            rec["Markt"] = rec["question"].apply(
                lambda q: (q[:60] + "…") if q and len(q) > 60 else (q or "—"))
            rec = rec.drop(columns=["question"])
        if "coin" in rec.columns:
            rec = rec.rename(columns={"coin": "Coin"})
        if "winner_side" in rec.columns:
            rec = rec.rename(columns={"winner_side": "Richting"})
        if "created_at" in rec.columns:
            rec = rec.rename(columns={"created_at": "Tijdstip"})

        st.dataframe(rec, use_container_width=True, hide_index=True)

        # Paginatieknoppen
        if total_count > PER_PAGE:
            nav_cols = st.columns([1, 1, 4, 1, 1])
            with nav_cols[0]:
                if st.button("⏮", disabled=(page == 0), key="st_page_first"):
                    st.session_state["st_trade_page"] = 0
                    st.rerun()
            with nav_cols[1]:
                if st.button("◀", disabled=(page == 0), key="st_page_prev"):
                    st.session_state["st_trade_page"] = max(0, page - 1)
                    st.rerun()
            with nav_cols[2]:
                st.markdown(
                    f'<div style="text-align:center;padding-top:6px;color:#9ca3af;font-size:13px">'
                    f'Pagina {page + 1} van {max_page + 1}'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            with nav_cols[3]:
                if st.button("▶", disabled=(page >= max_page), key="st_page_next"):
                    st.session_state["st_trade_page"] = min(max_page, page + 1)
                    st.rerun()
            with nav_cols[4]:
                if st.button("⏭", disabled=(page >= max_page), key="st_page_last"):
                    st.session_state["st_trade_page"] = max_page
                    st.rerun()
    else:
        st.caption("Nog geen voltooide trades in deze periode.")

    st.divider()

    # ── Instellingen (standaard ingeklapt) ──────────────────────────────────────
    with st.expander("⚙️ Instellingen", expanded=False):
        # Accuracy per coin uit Signal Lab — zelfde periode-filter als de hoofdview
        sl_accuracy  = get_signal_accuracy_per_coin(hours=_hours)
        sl_acc_map: dict[str, dict] = {r["coin"]: r for r in sl_accuracy}
        _period_label = period  # bv "1u", "7 dagen", "Alle tijd"

        with st.form("st_config_form"):
            col_l, col_r = st.columns(2)
            with col_l:
                conv_thr = st.slider(
                    "Conviction drempel",
                    min_value=0.10, max_value=0.90,
                    value=float(cfg.get("conviction_threshold", 0.35)),
                    step=0.05,
                    help="Minimum signaalsterkte om een trade te plaatsen. Lager = meer trades, hogere drempel = strengere selectie.",
                )
                _entry_max_val = float(cfg.get("entry_price_max", 0.58))
                entry_max = st.slider(
                    "Max entry prijs (ct per share)",
                    min_value=0.50, max_value=0.75,
                    value=_entry_max_val,
                    step=0.01, format="%.2f",
                    help="Nooit meer dan dit betalen per share bij entry. Bij in-window trading liggen prijzen vaak al op 0.55–0.70.",
                )
                _be = round(entry_max / 1.0 * 100, 1)
                st.caption(f"Break-even bij deze prijs: **{_be:.0f}% win rate** vereist")
            with col_r:
                trade_size = st.number_input(
                    "Inleg per trade (€)",
                    min_value=1.0, max_value=200.0,
                    value=float(cfg.get("trade_size_eur", 10.0)),
                    step=1.0,
                    help="Hoeveel EUR per trade geïnvesteerd wordt.",
                )
                max_conc = st.number_input(
                    "Max gelijktijdige posities",
                    min_value=1, max_value=50,
                    value=int(cfg.get("max_concurrent_positions", 10)),
                    step=1,
                    help="Maximaal aantal open signal_trader posities tegelijk (alle coins samen).",
                )

            st.markdown("---")
            st.markdown(f"**🪙 Coins** — Signal Lab accuracy ({_period_label})")
            st.caption("Break-even bij ~52%. Groen ≥ 55%, oranje 52–55%, rood < 52%.")

            coin_enabled_new: dict[str, bool] = {}
            for coin in ["BTC", "ETH", "SOL", "XRP", "DOGE"]:
                default_on = cfg.get("coins", {}).get(coin, {}).get("enabled", True)
                sl = sl_acc_map.get(coin, {})
                sl_n   = int(sl.get("n", 0))
                sl_acc = float(sl.get("accuracy_pct", 0.0)) if sl_n >= 3 else None
                sl_pnl = float(sl.get("total_pnl", 0.0))

                col_name, col_tog, col_info = st.columns([1, 1, 3])
                with col_name:
                    st.markdown(f"**{COIN_EMOJI.get(coin, '')} {coin}**")
                with col_tog:
                    coin_enabled_new[coin] = st.checkbox(
                        "Aan", value=default_on, key=f"st_coin_{coin}"
                    )
                with col_info:
                    if sl_acc is not None:
                        color = "#34d399" if sl_acc >= 55 else ("#fbbf24" if sl_acc >= 52 else "#f87171")
                        if sl_acc >= 55 and not default_on:
                            rec_icon = "✅ overweeg aan te zetten"
                        elif sl_acc < 52 and default_on:
                            rec_icon = "⚠️ overweeg uit te zetten"
                        elif sl_acc < 52 and not default_on:
                            rec_icon = "❌ laat uit"
                        elif sl_acc < 55:
                            rec_icon = "⚠️ grensgebied"
                        else:
                            rec_icon = ""
                        st.markdown(
                            f'<span style="color:{color};font-size:13px">'
                            f'<b>{sl_acc:.1f}%</b> signal accuracy &nbsp;·&nbsp; '
                            f'{sl_n} Signal Lab trades'
                            f'{(" &nbsp;→ " + rec_icon) if rec_icon else ""}'
                            f'</span>',
                            unsafe_allow_html=True,
                        )
                    else:
                        n_desc = f"{sl_n} trades" if sl_n else "geen data"
                        st.caption(f"te weinig Signal Lab data ({n_desc})")

            submitted = st.form_submit_button("💾 Opslaan & doorsturen naar bot", type="primary", use_container_width=True)

        if submitted:
            global_params = {
                "conviction_threshold":     conv_thr,
                "trade_size_eur":           trade_size,
                "entry_price_max":          entry_max,
                "max_concurrent_positions": max_conc,
            }
            coin_params = {coin: {"enabled": coin_enabled_new[coin]} for coin in coin_enabled_new}

            err = _save_signal_trader_to_yaml(global_params, coin_params)
            if not err:
                write_command("apply_signal_trader_config", {
                    **global_params,
                    "coins": coin_params,
                })
                st.success("✅ Instellingen opgeslagen en doorgestuurd naar de bot!")
                st.rerun()
            else:
                st.error(f"❌ Schrijffout config.yaml: {err}")

        # ── Verwijder signal trades ─────────────────────────────────────────────
        st.markdown("---")
        st.markdown("**🗑️ Trade-geschiedenis verwijderen**")
        col_del_ab, col_del_all = st.columns(2)
        with col_del_ab:
            if st.button("🗑️ Verwijder aborted trades", key="st_del_aborted"):
                n = delete_signal_trades(status="aborted")
                st.success(f"✅ {n} afgebroken trades verwijderd.")
                st.rerun()
        with col_del_all:
            if st.button("⚠️ Verwijder ALLE signal trades", key="st_del_all",
                         type="secondary"):
                n = delete_signal_trades()
                st.success(f"✅ {n} trades verwijderd — schone lei!")
                st.rerun()


@st.fragment
def _signal_trader_panel() -> None:
    _signal_trader_live()


# ── Loading screen (eerste keer dat deze sessie de pagina laadt) ───────────────
if not st.session_state.get("_page_loaded"):
    st.markdown("""
<div style="text-align:center;padding:60px 20px 20px">
  <h1 style="color:#93c5fd;margin-bottom:4px">📡 Poly-Baws-Bot</h1>
  <p style="color:#6b7280;font-size:14px">Dashboard wordt geladen…</p>
</div>
""", unsafe_allow_html=True)
    _pb = st.progress(0, text="Opstarten… 0%")

    # ── Stap 1: bot status (Live tab) ─────────────────────────────────────────
    _pb.progress(10, text="🔌 Database verbinding… 10%")
    _ = get_bot_heartbeat_age()

    # ── Stap 2: dagelijkse P&L + trade counts (Beveiliging) ───────────────────
    _pb.progress(20, text="📈 Dagelijkse P&L laden… 20%")
    for _lc_coin in COINS:
        get_daily_pnl(_lc_coin)
        get_today_trade_count(_lc_coin)

    # ── Stap 3: guard state per coin (Beveiliging) ────────────────────────────
    _pb.progress(35, text="🛡️ Beveiligingsstatus ophalen… 35%")
    for _lc_coin in COINS:
        get_state(f"cg_{_lc_coin}_state")
        get_state(f"cg_{_lc_coin}_streak")
        get_state(f"cg_{_lc_coin}_reason")

    # ── Stap 4: learning cyclus (Learning tab) ────────────────────────────────
    _pb.progress(50, text="🧠 Learning cyclus laden… 50%")
    _lc_cycle = get_current_cycle()
    get_latest_completed_cycle()
    if _lc_cycle and _lc_cycle.get("id"):
        get_learn_coin_counts(_lc_cycle["id"])

    # ── Stap 5: open posities + scanner (Live tab) ────────────────────────────
    _pb.progress(65, text="📂 Open posities ophalen… 65%")
    _ = get_open_trades()
    for _lc_coin in COINS:
        get_scanner_state(_lc_coin)

    # ── Stap 6: portfolio (Portfolio tab) ─────────────────────────────────────
    _pb.progress(80, text="💼 Portfolio laden… 80%")
    _ = get_portfolio_snapshot()

    # ── Stap 7: recente events (Live tab) ─────────────────────────────────────
    _pb.progress(92, text="📋 Recente events laden… 92%")
    get_recent_events(limit=50)
    get_recent_trades(limit=10)

    _pb.progress(100, text="✅ Dashboard gereed! 100%")

    import time as _time_mod
    _time_mod.sleep(0.4)   # brief pause so user sees 100%
    st.session_state["_page_loaded"] = True
    st.session_state["_tabs_ready"] = False  # tabs still need their own init pass
    st.rerun()             # → render 2: Beveiliging + Learning direct, rest skeleton

# ── Tab rendering — fase-gebaseerd ─────────────────────────────────────────────
# ── Whale Tracker helpers (config read/write) ─────────────────────────────────

_WHALE_CONFIG_PATH = Path(__file__).parent / "config" / "config.yaml"


def _whale_config_write(name: str, address: str) -> str:
    """Add whale to config.yaml. Returns error string or '' on success."""
    try:
        from ruamel.yaml import YAML
        ryaml = YAML()
        ryaml.preserve_quotes = True
        ryaml.width = 4096
        with open(_WHALE_CONFIG_PATH) as f:
            cfg = ryaml.load(f)
        cfg.setdefault("whale_tracker", {}).setdefault("addresses", {})[name] = address
        with open(_WHALE_CONFIG_PATH, "w") as f:
            ryaml.dump(cfg, f)
        return ""
    except ImportError:
        import yaml as _yaml
        try:
            with open(_WHALE_CONFIG_PATH) as f:
                cfg = _yaml.safe_load(f)
            cfg.setdefault("whale_tracker", {}).setdefault("addresses", {})[name] = address
            with open(_WHALE_CONFIG_PATH, "w") as f:
                _yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            return ""
        except Exception as exc:
            return str(exc)
    except Exception as exc:
        return str(exc)


def _whale_config_remove(name: str) -> str:
    """Remove whale from config.yaml. Returns error string or '' on success."""
    try:
        from ruamel.yaml import YAML
        ryaml = YAML()
        ryaml.preserve_quotes = True
        ryaml.width = 4096
        with open(_WHALE_CONFIG_PATH) as f:
            cfg = ryaml.load(f)
        cfg.get("whale_tracker", {}).get("addresses", {}).pop(name, None)
        with open(_WHALE_CONFIG_PATH, "w") as f:
            ryaml.dump(cfg, f)
        return ""
    except ImportError:
        import yaml as _yaml
        try:
            with open(_WHALE_CONFIG_PATH) as f:
                cfg = _yaml.safe_load(f)
            cfg.get("whale_tracker", {}).get("addresses", {}).pop(name, None)
            with open(_WHALE_CONFIG_PATH, "w") as f:
                _yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            return ""
        except Exception as exc:
            return str(exc)
    except Exception as exc:
        return str(exc)


def _whale_db_remove(address: str) -> None:
    """Delete whale data from DB tables."""
    from src.db_sync import _conn, _db_path
    if not _db_path.exists():
        return
    try:
        with _conn() as conn:
            conn.execute("DELETE FROM whale_meta WHERE address = ?", (address,))
            conn.execute("DELETE FROM whale_activity WHERE address = ?", (address,))
            conn.execute("DELETE FROM whale_positions WHERE address = ?", (address,))
    except Exception:
        pass


# ── Whale Tracker panel ───────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def _q_whale_meta():
    return get_whale_meta()

@st.cache_data(ttl=60)
def _q_whale_activity(address, coin, days):
    return get_whale_activity(address=address or None, coin=coin or None, days=days)

@st.cache_data(ttl=60)
def _q_whale_positions(address, coin, active_only):
    return get_whale_positions(address=address or None, coin=coin or None, active_only=active_only)

@st.cache_data(ttl=120)
def _q_whale_overlap(days):
    return get_whale_bot_overlap(days=days)


def _whale_account_card(m: dict, col_key_suffix: str) -> None:
    """Render one whale account card: stats + history load button."""
    import pandas as pd
    from src.whale_tracker import deep_sync_whale_sync

    name = m["name"]
    address = m["address"]
    addr_short = f"{address[:8]}…{address[-6:]}"
    history_loaded = bool(m.get("history_loaded", 0))
    act_count = m.get("activity_count", 0)
    pos_count = m.get("positions_count", 0)
    last_sync = (m.get("last_synced_at") or "")[:16]

    st.markdown(f"**{name}**  `{addr_short}`")
    c1, c2, c3 = st.columns(3)
    c1.metric("Transacties", act_count, help="Aantal transacties in database")
    c2.metric("Posities", pos_count, help="Huidige open/gesloten posities")
    hist_label = "✅ Volledig" if history_loaded else "⚠️ Gedeeltelijk"
    c3.metric("Historie", hist_label, help="Is de volledige geschiedenis geladen?")
    if last_sync:
        st.caption(f"Laatste sync: {last_sync}")

    # Buttons row
    btn_hist_key = f"wh_hist_{col_key_suffix}"
    b1, b2 = st.columns(2)
    with b1:
        if not history_loaded:
            if st.button("📥 Laad volledige historie", key=btn_hist_key, use_container_width=True):
                with st.spinner(f"Paginering door {name} — kan 20-30 seconden duren…"):
                    total, new_rows = deep_sync_whale_sync(name, address)
                st.success(f"Klaar: {total} transacties opgehaald, {new_rows} nieuw opgeslagen.")
                _q_whale_meta.clear()
                st.rerun()
        else:
            st.caption("✅ Volledige historische data geladen.")
    with b2:
        # CSV export — fetch all rows, no limit
        all_rows = get_whale_activity(address=address, limit=100_000)
        if all_rows:
            csv_df = pd.DataFrame(all_rows)
            export_cols = [c for c in ["event_ts", "coin", "outcome_side", "trade_type", "price", "usdc_size", "size", "question", "market_id", "transaction_hash"] if c in csv_df.columns]
            csv_bytes = csv_df[export_cols].to_csv(index=False).encode("utf-8")
            st.download_button(
                label="⬇️ Exporteer CSV",
                data=csv_bytes,
                file_name=f"{name}_activity.csv",
                mime="text/csv",
                key=f"wh_csv_{col_key_suffix}",
                use_container_width=True,
            )

    # Remove button (confirm via session state)
    _confirm_key = f"wh_del_confirm_{col_key_suffix}"
    if st.session_state.get(_confirm_key):
        st.warning(f"Verwijder **{name}** inclusief alle data?")
        rc1, rc2 = st.columns(2)
        if rc1.button("✅ Ja, verwijder", key=f"wh_del_yes_{col_key_suffix}", type="primary"):
            err = _whale_config_remove(name)
            if not err:
                _whale_db_remove(address)
                st.session_state.pop(_confirm_key, None)
                _q_whale_meta.clear()
                st.success(f"{name} verwijderd.")
                st.rerun()
            else:
                st.error(f"Fout: {err}")
        if rc2.button("✗ Annuleer", key=f"wh_del_no_{col_key_suffix}"):
            st.session_state.pop(_confirm_key, None)
            st.rerun()
    else:
        if st.button("🗑️ Verwijder whale", key=f"wh_del_{col_key_suffix}",
                     use_container_width=False):
            st.session_state[_confirm_key] = True
            st.rerun()

    st.divider()

    # Preview: last 50 rows in card
    rows = get_whale_activity(address=address, limit=50)
    if rows:
        df = pd.DataFrame(rows)
        show = [c for c in ["coin", "outcome_side", "trade_type", "price", "usdc_size", "event_ts"] if c in df.columns]
        df_s = df[show].copy()
        df_s.columns = [{"coin": "Coin", "outcome_side": "Kant", "trade_type": "Type",
                          "price": "Prijs", "usdc_size": "USDC", "event_ts": "Tijd"}.get(c, c) for c in show]
        if "Prijs" in df_s.columns:
            df_s["Prijs"] = df_s["Prijs"].apply(lambda x: f"{x:.3f}" if x is not None else "")
        if "USDC" in df_s.columns:
            df_s["USDC"] = df_s["USDC"].apply(lambda x: f"${x:.2f}" if x is not None else "")
        st.dataframe(df_s, hide_index=True, use_container_width=True, height=320)
    else:
        st.caption("Nog geen activiteit geladen.")


@st.fragment
def _whale_panel() -> None:
    import pandas as pd

    st.subheader("🐋 Whale Tracker")

    meta = _q_whale_meta()
    if not meta:
        st.info("Geen whale-adressen geconfigureerd of nog niet gesynct. Voeg adressen toe in `config.yaml → whale_tracker.addresses`.")
        return

    # ── Side-by-side account cards ───────────────────────────────────────────
    cols = st.columns(len(meta))
    for col, m in zip(cols, meta):
        with col:
            _whale_account_card(m, col_key_suffix=m["name"].replace(" ", "_"))

    # ── Gedeelde filterbalk ──────────────────────────────────────────────────
    col_addr, col_coin, col_days = st.columns([2, 1.5, 1.5])
    addr_options = ["Alle"] + [m["name"] for m in meta]
    with col_addr:
        sel_addr_label = st.selectbox("Gefilterd account", addr_options, key="wh_addr")
    with col_coin:
        sel_coin = st.selectbox("Coin", ["Alle", "BTC", "ETH", "SOL", "DOGE", "XRP"], key="wh_coin")
    with col_days:
        sel_days_label = st.selectbox("Periode", ["7 dagen", "30 dagen", "Alle"], key="wh_days")

    sel_addr = next((m["address"] for m in meta if m["name"] == sel_addr_label), None) if sel_addr_label != "Alle" else None
    sel_coin_val = None if sel_coin == "Alle" else sel_coin
    sel_days = {"7 dagen": 7, "30 dagen": 30, "Alle": None}[sel_days_label]

    # ── Detail tabs ──────────────────────────────────────────────────────────
    tab_act, tab_pos, tab_overlap = st.tabs(["📋 Alle activiteit", "💼 Posities", "🔀 Overlap met bot"])

    with tab_act:
        rows = _q_whale_activity(sel_addr, sel_coin_val, sel_days)
        if not rows:
            st.caption("Geen activiteit gevonden.")
        else:
            df = pd.DataFrame(rows)
            show_cols = [c for c in ["name", "coin", "outcome_side", "trade_type", "price", "usdc_size", "question", "event_ts"] if c in df.columns]
            df_show = df[show_cols].copy()
            df_show.columns = [{"name": "Account", "coin": "Coin", "outcome_side": "Kant", "trade_type": "Type",
                                 "price": "Prijs", "usdc_size": "USDC", "question": "Markt", "event_ts": "Tijd"}.get(c, c) for c in show_cols]
            if "Prijs" in df_show.columns:
                df_show["Prijs"] = df_show["Prijs"].apply(lambda x: f"{x:.3f}" if x is not None else "")
            if "USDC" in df_show.columns:
                df_show["USDC"] = df_show["USDC"].apply(lambda x: f"${x:.2f}" if x is not None else "")
            st.dataframe(df_show, hide_index=True, use_container_width=True)
            st.caption(f"{len(rows)} transacties")

    with tab_pos:
        rows = _q_whale_positions(sel_addr, sel_coin_val, False)
        if not rows:
            st.caption("Geen posities gevonden.")
        else:
            df = pd.DataFrame(rows)
            if "is_redeemable" in df.columns:
                active = df[df["is_redeemable"] == 0]
                expired = df[df["is_redeemable"] == 1]
            else:
                active = df
                expired = pd.DataFrame()
            show_cols = [c for c in ["name", "coin", "side", "size", "avg_price", "cur_price", "cash_pnl", "pct_pnl", "question"] if c in df.columns]
            if not active.empty:
                st.markdown("**Actieve posities**")
                df_a = active[show_cols].copy()
                df_a.columns = [{"name": "Account", "coin": "Coin", "side": "Kant", "size": "Shares",
                                  "avg_price": "Gem.prijs", "cur_price": "Nu", "cash_pnl": "P&L $",
                                  "pct_pnl": "P&L %", "question": "Markt"}.get(c, c) for c in show_cols]
                if "P&L %" in df_a.columns:
                    df_a["P&L %"] = df_a["P&L %"].apply(lambda x: f"{x:+.1f}%" if x is not None else "")
                if "P&L $" in df_a.columns:
                    df_a["P&L $"] = df_a["P&L $"].apply(lambda x: f"${x:+.2f}" if x is not None else "")
                st.dataframe(df_a, hide_index=True, use_container_width=True)
            if not expired.empty:
                with st.expander(f"Verlopen posities ({len(expired)}) — redeemable"):
                    st.dataframe(expired[show_cols], hide_index=True, use_container_width=True)

    with tab_overlap:
        st.caption("Markten waar een whale handelde terwijl jouw bot ook actief was (±30 min).")
        overlap = _q_whale_overlap(sel_days or 30)
        if not overlap:
            st.caption("Geen overlap gevonden in geselecteerde periode.")
        else:
            df = pd.DataFrame(overlap)
            show_cols = [c for c in ["coin", "bot_ts", "whale_name", "whale_side", "whale_price", "whale_usdc", "winner_side", "net_pnl"] if c in df.columns]
            df_show = df[show_cols].copy()
            df_show.columns = [{"coin": "Coin", "bot_ts": "Bot tijd", "whale_name": "Whale",
                                  "whale_side": "Whale kant", "whale_price": "Whale prijs",
                                  "whale_usdc": "Whale USDC", "winner_side": "Winnaar", "net_pnl": "Bot P&L"}.get(c, c) for c in show_cols]
            if "Bot P&L" in df_show.columns:
                df_show["Bot P&L"] = df_show["Bot P&L"].apply(lambda x: f"€{x:+.3f}" if x is not None else "")
            if "Whale prijs" in df_show.columns:
                df_show["Whale prijs"] = df_show["Whale prijs"].apply(lambda x: f"{x:.3f}" if x is not None else "")
            if "Whale USDC" in df_show.columns:
                df_show["Whale USDC"] = df_show["Whale USDC"].apply(lambda x: f"${x:.2f}" if x is not None else "")
            st.dataframe(df_show, hide_index=True, use_container_width=True)
            st.caption(f"{len(overlap)} overlappende trades")

    # ── Whale toevoegen ──────────────────────────────────────────────────────
    st.divider()
    with st.expander("➕ Whale toevoegen", expanded=not meta):
        st.caption("Voeg een Polymarket-adres toe. De bot synchroniseert de activiteit automatisch (eerste sync ≤5 min).")
        with st.form("wh_add_form", clear_on_submit=True):
            wh_name_in = st.text_input(
                "Naam (intern label)",
                placeholder="bijv. crypto_whale_1",
                help="Gebruik alleen letters, cijfers en underscores. Wordt gebruikt als label in de UI.",
            )
            wh_addr_in = st.text_input(
                "Polymarket adres (0x…)",
                placeholder="0x2c5aad8f0a9fb039bf4417250b52a62c3b95ef11",
            )
            submitted = st.form_submit_button("➕ Toevoegen", type="primary", use_container_width=True)

        if submitted:
            wh_name_clean = wh_name_in.strip().replace(" ", "_")
            wh_addr_clean = wh_addr_in.strip().lower()
            if not wh_name_clean:
                st.error("Vul een naam in.")
            elif not wh_addr_clean.startswith("0x") or len(wh_addr_clean) != 42:
                st.error("Ongeldig adres — moet beginnen met 0x en 42 tekens lang zijn.")
            elif wh_name_clean in [m["name"] for m in (meta or [])]:
                st.error(f"Naam '{wh_name_clean}' bestaat al.")
            elif wh_addr_clean in [m["address"].lower() for m in (meta or [])]:
                st.warning("Dit adres is al toegevoegd.")
            else:
                err = _whale_config_write(wh_name_clean, wh_addr_clean)
                if err:
                    st.error(f"Config schrijven mislukt: {err}")
                else:
                    _q_whale_meta.clear()
                    st.success(
                        f"**{wh_name_clean}** toegevoegd. "
                        "De bot synchroniseert de activiteit bij de volgende sync-cyclus (≤5 min). "
                        "Klik daarna op 📥 Laad volledige historie voor alle historische data."
                    )
                    st.rerun()


# Render 2 (_tabs_ready=False): Learning + Beveiliging direct, andere tabs tonen
#   een laadindicator. Aan het einde st.rerun() → render 3 volledig dashboard.
# Render 3+ (_tabs_ready=True): alles normaal.
# Safety: if _page_loaded was just set (loading screen ran this render), _tabs_ready
# was explicitly set to False by the loading screen and we skip this default.
# In all other cases (fresh session after restart, hot-reload), default to True so
# the app never gets stuck on skeletons indefinitely.
_tabs_ready = st.session_state.get("_tabs_ready", True)  # True = niet eerste keer

_all_tab_names = ["🧠 BGGDSB", "🔴 Live", "🎯 Signal Trader", "🤖 Auto Router",
                  "🔬 Signal Lab", "🧠 Learning", "🛡️ Beveiliging",
                  "📊 Analytics", "💼 Portfolio", "🐋 Whales"]
_all_tabs = st.tabs(_all_tab_names)

# Map tab objects by name
_tab_map = dict(zip(_all_tab_names, _all_tabs))

tab_bggdsb    = _tab_map["🧠 BGGDSB"]
tab_live      = _tab_map["🔴 Live"]
tab_st        = _tab_map["🎯 Signal Trader"]
tab_ar        = _tab_map["🤖 Auto Router"]
tab_signal_lab = _tab_map["🔬 Signal Lab"]
tab_learning  = _tab_map["🧠 Learning"]
tab_guard     = _tab_map["🛡️ Beveiliging"]

with tab_bggdsb:
    if _tabs_ready:
        bggdsb_panel()
    else:
        _tab_loading("🧠 BGGDSB", "BGGDSB strategie data wordt geladen…")

with tab_live:
    if _tabs_ready:
        dashboard()
        dashboard_event_log()
    else:
        _tab_loading("🔴 Live", "Handelsdata en open posities worden geladen…")

with tab_st:
    if _tabs_ready:
        _signal_trader_panel()
    else:
        _tab_loading("🎯 Signal Trader", "Signal Trader data wordt geladen…")

with tab_ar:
    if _tabs_ready:
        _auto_router_panel()
    else:
        _tab_loading("🤖 Auto Router", "Router configuratie wordt geladen…")

with tab_signal_lab:
    if _tabs_ready:
        signal_lab_panel()
    else:
        _tab_loading("🔬 Signal Lab", "Trade-data en signalen worden geladen…")

with tab_learning:
    _learning_panel()

with tab_guard:
    coin_protection_panel()

tab_analytics = _tab_map["📊 Analytics"]
tab_portfolio = _tab_map["💼 Portfolio"]
tab_whale     = _tab_map["🐋 Whales"]

with tab_analytics:
    if _tabs_ready:
        analytics_panel()
    else:
        _tab_loading("📊 Analytics", "Handelsanalyse en grafieken worden geladen…")

with tab_portfolio:
    if _tabs_ready:
        _portfolio_panel()
    else:
        _tab_loading("💼 Portfolio", "Portfolio snapshot wordt geladen…")

with tab_whale:
    if _tabs_ready:
        _whale_panel()
    else:
        _tab_loading("🐋 Whales", "Whale data wordt geladen…")

# ── Auto-advance naar volledig dashboard ───────────────────────────────────────
# Na render 2 (skeleton pass) → trigger render 3 (volledig).
# Na render 3+ doet dit niets (_tabs_ready is al True).
if not _tabs_ready:
    st.session_state["_tabs_ready"] = True
    st.rerun()

# Trade detail dialog — rendered outside all fragments/tabs so it is not
# affected by the dashboard fragment's 5-second auto-refresh.
_detail_id = st.session_state.pop("_detail_trade_id", None)
_detail_allow_closed = st.session_state.pop("_detail_allow_closed", False)
if _detail_id:
    _all_open = get_open_trades()
    _detail_trade = next((t for t in _all_open if t.get("trade_id") == _detail_id), None)
    if _detail_trade is None and _detail_allow_closed:
        _router = get_router_trades(hours=48, limit=200)
        _detail_trade = next((t for t in _router if t.get("trade_id") == _detail_id), None)
    if _detail_trade:
        _trade_detail_dialog(_detail_trade)

