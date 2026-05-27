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
    get_phase_stats,
    get_portfolio_snapshot,
    get_recent_events,
    get_recent_trades,
    get_scanner_alerts,
    get_scanner_state,
    get_state,
    get_today_trade_count,
)
from src.commands import write_command, delete_hybrid_pending
from src.risk import KILL_FLAG_PATH
from analytics_tab import analytics_panel
from signal_lab_tab import signal_lab_panel
from coin_protection_tab import coin_protection_panel

COINS = list(CONFIG["coins"].keys())
COIN_EMOJI = {"BTC": "₿", "ETH": "Ξ", "SOL": "◎", "XRP": "✕", "DOGE": "Ð"}
MODES = ["paper_hybrid", "paper_auto", "live_hybrid", "live_auto", "live_learning"]

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


# ── Sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## ₿ Poly-Baws-Bot")
    st.markdown(bot_status_html(), unsafe_allow_html=True)
    st.divider()

    st.markdown("**Mode**")
    mode = current_mode()
    mode_idx = MODES.index(mode) if mode in MODES else 0
    new_mode = st.radio("mode", MODES, index=mode_idx, label_visibility="collapsed")
    if new_mode != mode:
        write_command("set_mode", {"mode": new_mode})
        st.rerun()

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

    if mode.startswith("paper"):
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
        st.warning("⏸ BOT GEPAUZEERD — draait maar handelt niet")
        if st.button("▶ RESUME TRADING", use_container_width=True, type="primary"):
            write_command("reset_kill")
            st.rerun()
    else:
        st.success("🟢 BOT LIVE — trading actief")
        if st.button("⏸ PAUSE TRADING", use_container_width=True, type="secondary"):
            write_command("kill")
            st.rerun()

    st.divider()

    st.markdown("**Trade size (EUR per leg)**")
    saved_eur = get_state("trade_size_eur")
    db_eur = float(saved_eur) if saved_eur else CONFIG["trading"].get("trade_size_eur", 1.0)
    # Keep a pending value in session_state while the bot processes the command.
    # Clear it once the DB reflects the change.
    _ts_key = "_pending_trade_size_eur"
    pending = st.session_state.get(_ts_key)
    if pending is not None and abs(pending - db_eur) < 0.001:
        del st.session_state[_ts_key]
    display_eur = st.session_state.get(_ts_key, db_eur)
    new_eur = st.number_input(
        "trade_size_input",
        min_value=0.50,
        max_value=100.0,
        value=display_eur,
        step=0.10,
        format="%.2f",
        label_visibility="collapsed",
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
    new_scalein = st.number_input(
        "max_scalein_input",
        min_value=0.0,
        max_value=100.0,
        value=display_scalein,
        step=0.10,
        format="%.2f",
        label_visibility="collapsed",
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


# ── Main dashboard (auto-refresh every 5s) ────────────────────────────────────

@st.fragment(run_every=5)
def dashboard() -> None:
    _scanner_alerts()
    _hybrid_panel()
    open_trades = get_open_trades()
    _coin_grid(open_trades)
    st.divider()
    col_left, col_right = st.columns(2)
    with col_left:
        _active_positions(open_trades)
    with col_right:
        _recent_trades()


def dashboard_event_log() -> None:
    st.divider()
    _event_log()


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
    if _guard_state == "disabled":
        if not st.session_state.get(f"_gd_card_{coin}"):
            _ga, _gb = st.columns([5, 1])
            with _ga:
                st.error("🚫 Guard — zie Beveiliging", icon=None)
            with _gb:
                if st.button("✕", key=f"gd_card_x_{coin}", help="Verberg"):
                    st.session_state[f"_gd_card_{coin}"] = True
                    st.rerun()
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
            f"{COIN_EMOJI.get(c,'')} {c} ({r.get('match',{}).get('win_pct',0):.1f}%)"
            for c, r in pm_results.items()
            if r.get("match", {}).get("win_pct", 100) < 52
            and r.get("match", {}).get("n", 0) >= 10
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


def _portfolio_panel() -> None:
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


tab_live, tab_analytics, tab_signal_lab, tab_learning, tab_portfolio, tab_guard = st.tabs(
    ["🔴 Live", "📊 Analytics", "🔬 Signal Lab", "🧠 Learning", "💼 Portfolio", "🛡️ Beveiliging"]
)
with tab_live:
    dashboard()
    dashboard_event_log()
with tab_analytics:
    analytics_panel()
with tab_signal_lab:
    signal_lab_panel()
with tab_learning:
    _learning_panel()
with tab_portfolio:
    _portfolio_panel()
with tab_guard:
    coin_protection_panel()

# Trade detail dialog — rendered outside all fragments/tabs so it is not
# affected by the dashboard fragment's 5-second auto-refresh.
_detail_id = st.session_state.pop("_detail_trade_id", None)
if _detail_id:
    _all_open = get_open_trades()
    _detail_trade = next((t for t in _all_open if t.get("trade_id") == _detail_id), None)
    if _detail_trade:
        _trade_detail_dialog(_detail_trade)

