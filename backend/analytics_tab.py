"""Analytics tab for the Poly-Baws-Bot Streamlit dashboard."""
import pandas as pd
import streamlit as st

from src.config_loader import CONFIG
from src.commands import write_command
from src.db_sync import (
    get_analytics_trades,
    get_coin_comparison,
    get_exit_reason_stats,
    get_hourly_pnl,
    save_manual_analysis_to_db,
)
from src.claude_analyzer import analyze_trades_sync

COINS = list(CONFIG["coins"].keys())
COIN_EMOJI = {"BTC": "₿", "ETH": "Ξ", "SOL": "◎", "XRP": "✕", "DOGE": "Ð"}
_RANGE_DAYS = {"All time": None, "30 days": 30, "7 days": 7, "Today": 1}


def analytics_panel() -> None:
    # ── Global Filters ────────────────────────────────────────────────────────
    col_coin, col_range, col_export = st.columns([2, 3, 1])
    with col_coin:
        selected_coin = st.selectbox("Coin", ["All"] + COINS, key="an_coin")
    with col_range:
        selected_range = st.radio(
            "Period", list(_RANGE_DAYS.keys()), horizontal=True, key="an_range"
        )

    coin_filter = selected_coin if selected_coin != "All" else None
    days = _RANGE_DAYS[selected_range]

    trades = get_analytics_trades(coin=coin_filter, days=days)
    df = pd.DataFrame(trades) if trades else pd.DataFrame()

    with col_export:
        st.markdown("<br>", unsafe_allow_html=True)
        if not df.empty:
            export_cols = [
                "trade_id", "coin", "mode", "triggered_by",
                "window_start_ts", "window_end_ts",
                "entry_yes_price", "entry_no_price",
                "loser_exit_price", "winner_exit_price", "winner_exit_reason",
                "peak_bid", "ratchet_count", "time_in_trail_seconds",
                "fees_paid", "gross_pnl", "net_pnl", "created_at",
            ]
            available = [c for c in export_cols if c in df.columns]
            st.download_button(
                "📥 CSV", df[available].to_csv(index=False),
                "trades_export.csv", "text/csv", key="an_csv",
            )

    # ── Claude Analysis (always visible, even without trades) ─────────────────
    _claude_analysis_section(df, coin_filter, days)

    if df.empty:
        st.info("No completed triggered trades in this period.")
        return

    # ── Key Metrics ───────────────────────────────────────────────────────────
    _key_metrics(df)

    # ── Exit Reason Breakdown ─────────────────────────────────────────────────
    _exit_breakdown(coin_filter, days)

    # ── Per-Coin Comparison ───────────────────────────────────────────────────
    if coin_filter is None:
        _coin_comparison(days)

    # ── Cumulative P&L Chart ──────────────────────────────────────────────────
    _cumulative_pnl(df)

    # ── Trailing Metrics ──────────────────────────────────────────────────────
    _trailing_metrics(df)

    # ── Hourly Analysis ───────────────────────────────────────────────────────
    _hourly_analysis(coin_filter, days)

    # ── Full Trade History ────────────────────────────────────────────────────
    _trade_history(df)


# ── Sections ──────────────────────────────────────────────────────────────────

def _key_metrics(df: pd.DataFrame) -> None:
    st.markdown("### Key Metrics")
    total = len(df)
    triggered = df[df["trigger_hit"] == 1] if "trigger_hit" in df.columns else df
    aborted = df[df["status"].isin(["aborted"])] if "status" in df.columns else pd.DataFrame()
    closed = df[df["status"].isin(["closed", "resolved"])] if "status" in df.columns else df
    triggered_closed = closed[closed["trigger_hit"] == 1] if "trigger_hit" in closed.columns else closed

    winners = int((triggered_closed["net_pnl"] > 0).sum()) if not triggered_closed.empty else 0
    win_rate = winners / len(triggered_closed) * 100 if len(triggered_closed) else 0
    net_pnl = float(closed["net_pnl"].sum()) if not closed.empty else 0.0
    avg_pnl = float(triggered_closed["net_pnl"].mean()) if not triggered_closed.empty else 0.0
    cum = closed.sort_values("created_at")["net_pnl"].cumsum() if not closed.empty else pd.Series([0])
    max_dd = float((cum - cum.cummax()).min())

    c = st.columns(6)
    c[0].metric("Total Trades", total)
    c[1].metric("Triggered", len(triggered))
    c[2].metric("Aborted", len(aborted))
    c[3].metric("Win Rate", f"{win_rate:.1f}%")
    c[4].metric("Net P&L", f"€{net_pnl:+.2f}")
    c[5].metric("Avg P&L/Trade", f"€{avg_pnl:+.4f}")


def _exit_breakdown(coin: str | None, days: int | None) -> None:
    st.markdown("### Exit Reason Breakdown")
    stats = get_exit_reason_stats(coin=coin, days=days)
    if not stats:
        st.caption("No data.")
        return

    sdf = pd.DataFrame(stats)
    total_trades = sdf["count"].sum()
    sdf.insert(2, "pct", (sdf["count"] / total_trades * 100).round(1).astype(str) + "%")

    col_tbl, col_chart = st.columns([3, 2])
    with col_tbl:
        display = sdf.rename(columns={
            "winner_exit_reason": "Exit Reason",
            "count": "Count", "pct": "%",
            "avg_pnl": "Avg P&L", "total_pnl": "Total P&L",
            "avg_peak_bid": "Avg Peak Bid",
            "avg_ratchets": "Avg Ratchets",
            "avg_trail_time": "Avg Trail (s)",
        })
        st.dataframe(display, use_container_width=True, hide_index=True)
    with col_chart:
        st.bar_chart(sdf.set_index("winner_exit_reason")[["count"]])


def _coin_comparison(days: int | None) -> None:
    st.markdown("### Per-Coin Comparison")
    rows = get_coin_comparison(days=days)
    if not rows:
        st.caption("No data.")
        return

    cdf = pd.DataFrame(rows)
    cdf.insert(0, "", [COIN_EMOJI.get(c, "") for c in cdf["coin"]])
    cdf = cdf.rename(columns={
        "coin": "Coin", "trades": "Trades", "win_rate": "Win %",
        "total_pnl": "Net P&L", "avg_pnl": "Avg P&L",
        "avg_peak_bid": "Avg Peak Bid", "avg_trail_time": "Avg Trail (s)",
        "avg_fees": "Avg Fees",
    })
    st.dataframe(cdf, use_container_width=True, hide_index=True)


def _cumulative_pnl(df: pd.DataFrame) -> None:
    st.markdown("### Cumulative P&L")
    plot_df = df.sort_values("created_at").copy()
    plot_df["created_at"] = pd.to_datetime(plot_df["created_at"])

    chart: pd.DataFrame = pd.DataFrame()
    for coin in sorted(plot_df["coin"].unique()):
        sub = plot_df[plot_df["coin"] == coin].set_index("created_at")[["net_pnl"]].copy()
        sub = sub.rename(columns={"net_pnl": coin})
        sub[coin] = sub[coin].cumsum()
        chart = chart.join(sub, how="outer") if not chart.empty else sub

    total = plot_df.set_index("created_at")[["net_pnl"]].cumsum().rename(columns={"net_pnl": "Total"})
    chart = chart.join(total, how="outer") if not chart.empty else total
    st.line_chart(chart.ffill().fillna(0))


def _trailing_metrics(df: pd.DataFrame) -> None:
    triggered = df[df["trigger_hit"] == 1].copy() if "trigger_hit" in df.columns else df.copy()
    if triggered.empty:
        return

    st.markdown("### Trailing Strategy Metrics")
    c = st.columns(4)
    avg_trail = triggered["time_in_trail_seconds"].mean() if "time_in_trail_seconds" in triggered else None
    avg_peak = triggered["peak_bid"].mean() if "peak_bid" in triggered else None
    avg_ratchet = triggered["ratchet_count"].mean() if "ratchet_count" in triggered else None
    avg_fees = triggered["fees_paid"].mean() if "fees_paid" in triggered else None

    c[0].metric("Avg Trail Time", f"{avg_trail:.1f}s" if pd.notna(avg_trail) else "—")
    c[1].metric("Avg Peak Bid", f"{float(avg_peak):.4f}" if pd.notna(avg_peak) else "—")
    c[2].metric("Avg Ratchets", f"{avg_ratchet:.1f}" if pd.notna(avg_ratchet) else "—")
    c[3].metric("Avg Fees", f"€{float(avg_fees):.4f}" if pd.notna(avg_fees) else "—")

    scatter = triggered[["time_in_trail_seconds", "net_pnl", "winner_exit_reason"]].dropna()
    if not scatter.empty:
        st.markdown("**Trail Time vs P&L**")
        st.scatter_chart(scatter, x="time_in_trail_seconds", y="net_pnl", color="winner_exit_reason")


def _hourly_analysis(coin: str | None, days: int | None) -> None:
    st.markdown("### Avg P&L by Hour of Day (UTC)")
    hourly = get_hourly_pnl(coin=coin, days=days)
    if not hourly:
        st.caption("Not enough data yet (needs 100+ trades for reliable signal).")
        return
    hdf = pd.DataFrame(hourly).set_index("hour_utc")
    col_pnl, col_acc = st.columns(2)
    with col_pnl:
        st.caption("Avg P&L per hour")
        st.bar_chart(hdf[["avg_pnl"]])
    with col_acc:
        st.caption("Direction accuracy % per hour")
        acc = hdf[["direction_accuracy"]].dropna()
        if not acc.empty:
            st.bar_chart(acc)
        else:
            st.caption("No direction accuracy data yet.")


def _trade_history(df: pd.DataFrame) -> None:
    with st.expander(f"📋 Full Trade History ({len(df)} trades)", expanded=False):
        display_cols = [
            "created_at", "coin", "status", "mode", "triggered_by",
            "entry_yes_price", "entry_no_price",
            "loser_exit_price", "winner_exit_price", "winner_exit_reason",
            "actual_winner", "peak_bid", "ratchet_count", "time_in_trail_seconds",
            "fees_paid", "net_pnl",
        ]
        available = [c for c in display_cols if c in df.columns]
        show = df[available].copy().sort_values("created_at", ascending=False).head(500)
        if "created_at" in show.columns:
            show["created_at"] = pd.to_datetime(show["created_at"]).dt.strftime("%m-%d %H:%M")
        st.dataframe(show, use_container_width=True, hide_index=True)


def _build_current_params() -> dict:
    return {
        "coins": {
            coin: {
                "trigger_threshold": cfg.get("trigger_threshold", CONFIG["trading"]["trigger_threshold"]),
                "enabled": cfg.get("enabled", True),
            }
            for coin, cfg in CONFIG["coins"].items()
        },
        "exit": {
            "cross_threshold": CONFIG["exit"]["cross_threshold"],
            "initial_offset": CONFIG["exit"]["initial_offset"],
            "ratchet_buffer": CONFIG["exit"]["ratchet_buffer"],
        },
        "entry": {
            "max_combined_cost": CONFIG["entry"]["max_combined_cost"],
            "max_token_spread": CONFIG["entry"]["max_token_spread"],
        },
    }


def _claude_analysis_section(df: pd.DataFrame, coin: str | None, days: int | None) -> None:
    st.markdown("### 🤖 Claude Analyse")

    if st.button("Analyseer met Claude", key="an_claude_btn"):
        try:
            with st.spinner("Claude analyseert..."):
                params = _build_current_params()
                result = analyze_trades_sync(df.to_dict("records"), params)
            st.session_state["manual_analysis"] = result
            st.success("Analyse klaar!")
        except Exception as exc:
            st.error(f"Analyse mislukt: {exc}")

    result = st.session_state.get("manual_analysis")
    if not result:
        return

    conf = result.get("confidence_score", 0)
    st.metric("Vertrouwen", f"{conf * 100:.0f}%")
    reasoning = result.get("reasoning", "")
    if reasoning:
        st.markdown(reasoning)

    coin_params = result.get("coin_params", {})
    if coin_params:
        rows = []
        for c, cp in coin_params.items():
            rows.append({
                "Coin": c,
                "Trigger": cp.get("trigger_threshold", "—"),
                "Cross": cp.get("cross_threshold", "—"),
                "Offset": cp.get("initial_offset", "—"),
                "Buffer": cp.get("ratchet_buffer", "—"),
                "Enabled": cp.get("enabled", True),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    if st.button("✅ Toepassen op live_auto", key="an_apply_btn"):
        try:
            save_manual_analysis_to_db(result)
            write_command("toggle_learned_params", {"enabled": True})
            st.success("Parameters opgeslagen en toegepast op live_auto!")
            st.rerun()
        except Exception as exc:
            st.error(f"Opslaan mislukt: {exc}")
