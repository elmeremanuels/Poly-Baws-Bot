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
    get_open_trades,
    get_phase_stats,
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

COINS = list(CONFIG["coins"].keys())
COIN_EMOJI = {"BTC": "₿", "ETH": "Ξ", "SOL": "◎", "XRP": "✕", "DOGE": "Ð"}
MODES = ["paper_hybrid", "paper_auto", "live_hybrid", "live_auto", "live_learning"]

st.set_page_config(
    page_title="Poly-Baws-Bot",
    page_icon="₿",
    layout="wide",
    initial_sidebar_state="expanded",
)

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
    default_eur = float(saved_eur) if saved_eur else CONFIG["trading"].get("trade_size_eur", 1.0)
    new_eur = st.number_input(
        "trade_size_input",
        min_value=0.50,
        max_value=100.0,
        value=default_eur,
        step=0.10,
        format="%.2f",
        label_visibility="collapsed",
    )
    if abs(new_eur - default_eur) > 0.001:
        write_command("set_trade_size", {"trade_size_eur": new_eur})
        st.rerun()

    st.divider()

    daily_pnl = get_daily_pnl()
    total_trades = get_today_trade_count()
    st.metric("Today's P&L", fmt_eur(daily_pnl))
    st.metric("Today's trades", total_trades)

    st.divider()
    st.caption("Auto-refreshes every 5s")


# ── Main dashboard (auto-refresh every 5s) ────────────────────────────────────

@st.fragment(run_every=5)
def dashboard() -> None:
    _scanner_alerts()
    _hybrid_panel()
    _coin_grid()
    st.divider()
    col_left, col_right = st.columns(2)
    with col_left:
        _active_positions()
    with col_right:
        _recent_trades()
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


def _coin_grid() -> None:
    st.markdown("### Coins")
    open_trades = get_open_trades()
    online = bot_is_online()
    cols = st.columns(5)
    for i, coin in enumerate(COINS):
        with cols[i]:
            _coin_card(coin, open_trades, online)


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

    st.markdown(f"#### {COIN_EMOJI.get(coin, '')} {coin}")
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


def _active_positions() -> None:
    st.markdown("#### Active Positions")
    trades = get_open_trades()
    if not trades:
        st.caption("No active positions.")
        return

    rows = []
    for t in trades:
        be = t.get("break_even_price")
        rows.append({
            "Coin": t.get("coin", ""),
            "Status": t.get("status", ""),
            "Window": fmt_time(t.get("window_start_ts")),
            "YES entry": f"€{t['entry_yes_price']:.2f}" if t.get("entry_yes_price") else "—",
            "NO entry": f"€{t['entry_no_price']:.2f}" if t.get("entry_no_price") else "—",
            "Break-even": f"€{be:.2f}" if be is not None else "—",
            "Winner": t.get("winner_side") or "—",
            "Mode": t.get("mode", ""),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


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

    # Claude's analysis
    if cycle.get("claude_analysis"):
        with st.expander("Claude's latest reasoning"):
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

    # Control buttons
    st.markdown("---")
    st.markdown("**Manual controls**")
    c1, c2, c3 = st.columns(3)
    if c1.button("Force next phase", use_container_width=True):
        write_command("force_next_phase")
        st.toast("Phase transition requested.", icon="⏭")
    if c2.button("Reset cycle", use_container_width=True):
        write_command("reset_learning_cycle")
        st.toast("Cycle reset requested.", icon="🔄")
    if c3.button("Pause learning", use_container_width=True):
        write_command("kill")
        st.toast("Bot paused.", icon="⏸")


tab_live, tab_analytics, tab_learning = st.tabs(["🔴 Live", "📊 Analytics", "🧠 Learning"])
with tab_live:
    dashboard()
with tab_analytics:
    analytics_panel()
with tab_learning:
    _learning_panel()
