"""Poly-Baws-Bot Streamlit dashboard."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

from src.config_loader import CONFIG
from src.db_sync import (
    get_bot_heartbeat_age,
    get_daily_pnl,
    get_hybrid_pending,
    get_open_trades,
    get_recent_events,
    get_recent_trades,
    get_scanner_alerts,
    get_state,
    get_today_trade_count,
)
from src.commands import write_command, delete_hybrid_pending
from src.risk import KILL_FLAG_PATH

COINS = list(CONFIG["coins"].keys())
COIN_EMOJI = {"BTC": "₿", "ETH": "Ξ", "SOL": "◎", "XRP": "✕", "DOGE": "Ð"}
MODES = ["paper_hybrid", "paper_auto", "live_hybrid", "live_auto"]

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
        return '<span class="status-err">🛑 KILLED</span>'
    if age is None:
        return '<span class="status-warn">? Unknown</span>'
    if age < 15:
        return f'<span class="status-ok">● Running</span>'
    return f'<span class="status-warn">⚠ Stale ({age:.0f}s ago)</span>'


def fmt_eur(v: float) -> str:
    return f"{'+'if v >= 0 else ''}€{v:.2f}"


def fmt_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
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

    st.divider()

    killed = is_killed()
    if killed:
        if st.button("✅ RESUME BOT", use_container_width=True, type="primary"):
            write_command("reset_kill")
            st.rerun()
        st.warning("Bot is KILLED — no new trades.")
    else:
        if st.button("🛑 KILL SWITCH", use_container_width=True, type="secondary"):
            write_command("kill")
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
    alerts = get_scanner_alerts(3)
    for a in alerts:
        try:
            data = json.loads(a.get("data") or "{}")
        except Exception:
            data = {}
        coin = a.get("coin") or "?"
        reason = data.get("reason", "unknown")
        flt = data.get("filter", "")
        ts = (a.get("ts") or "")[:19]
        st.markdown(
            f'<div class="alert-banner">🔴 <b>SCANNER ALERT</b> — {coin}: {reason}'
            f'{f" (filter={flt})" if flt else ""}'
            f' <span style="color:#7f1d1d;float:right">{ts}</span></div>',
            unsafe_allow_html=True,
        )


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
    cols = st.columns(5)
    for i, coin in enumerate(COINS):
        with cols[i]:
            _coin_card(coin)


def _coin_card(coin: str) -> None:
    cfg = CONFIG["coins"][coin]
    pnl = get_daily_pnl(coin)
    count = get_today_trade_count(coin)

    enabled_str = get_state(f"coin_{coin}_enabled")
    enabled = cfg["enabled"] if enabled_str is None else (enabled_str.lower() == "true")
    max_str = get_state(f"coin_{coin}_max")
    max_p = int(max_str) if max_str else cfg["max_parallel_positions"]

    # Use session_state as write buffer so we don't re-send the same command
    # every 5s refresh while the bot hasn't processed it yet
    ss_max_key = f"sent_max_{coin}"
    ss_en_key = f"sent_en_{coin}"
    display_max = st.session_state.get(ss_max_key, max_p)
    display_en = st.session_state.get(ss_en_key, enabled)

    pnl_color = "#34d399" if pnl >= 0 else "#f87171"

    st.markdown(f"#### {COIN_EMOJI.get(coin, '')} {coin}")

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
        rows.append({
            "Coin": t.get("coin", ""),
            "Status": t.get("status", ""),
            "Window": fmt_time(t.get("window_start_ts")),
            "YES entry": f"€{t['entry_yes_price']:.2f}" if t.get("entry_yes_price") else "—",
            "NO entry": f"€{t['entry_no_price']:.2f}" if t.get("entry_no_price") else "—",
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
        rows.append({
            "Coin": t.get("coin", ""),
            "Exit": t.get("winner_exit_reason") or t.get("status", ""),
            "Mode": (t.get("mode") or "").replace("_", " "),
            "By": t.get("triggered_by", ""),
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
                "Time": (e.get("ts") or "")[:19],
                "Type": e.get("event_type", ""),
                "Coin": e.get("coin") or "—",
                "Data": (e.get("data") or "")[:120],
            })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.caption("No events.")


dashboard()
