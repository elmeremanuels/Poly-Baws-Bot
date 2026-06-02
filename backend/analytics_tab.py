"""Analytics tab for the Poly-Baws-Bot Streamlit dashboard."""
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from src.config_loader import CONFIG
from src.commands import write_command
from src.db_sync import (
    get_analytics_trades,
    get_coin_comparison,
    get_conviction_bucket_stats,
    get_conviction_threshold_sweep,
    get_exit_reason_stats,
    get_hourly_pnl,
    get_mode_comparison,
    get_ofi_bucket_stats,
    get_pnl_by_exit_reason,
    get_regime_bucket_stats,
    save_manual_analysis_to_db,
)
from src.claude_analyzer import analyze_trades_sync
from src.backtest_engine import BacktestEngine, BacktestResult

COINS = list(CONFIG["coins"].keys())
COIN_EMOJI = {"BTC": "₿", "ETH": "Ξ", "SOL": "◎", "XRP": "✕", "DOGE": "Ð"}

# Standaard: laatste 24 uur. Overige periodes via zoekfilter.
_RANGE_DAYS = {"24 uur": 1, "7 dagen": 7, "30 dagen": 30, "Alle tijd": None, "Aangepast": "custom"}


# ── Gecachede DB-queries ───────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def _q_trades(coin, days, only_today, mode_filter, date_from=None, date_to=None):
    return get_analytics_trades(coin=coin, days=days, only_today=only_today,
                                mode_filter=mode_filter, date_from=date_from, date_to=date_to)


@st.cache_data(ttl=60)
def _q_exit_stats(coin, days, only_today, mode_filter, date_from=None, date_to=None):
    return get_exit_reason_stats(coin=coin, days=days, only_today=only_today,
                                 mode_filter=mode_filter, date_from=date_from, date_to=date_to)


@st.cache_data(ttl=60)
def _q_coin_comparison(days, only_today, mode_filter, date_from=None, date_to=None):
    return get_coin_comparison(days=days, only_today=only_today,
                               mode_filter=mode_filter, date_from=date_from, date_to=date_to)


@st.cache_data(ttl=60)
def _q_hourly_pnl(coin, days, only_today, mode_filter, date_from=None, date_to=None):
    return get_hourly_pnl(coin=coin, days=days, only_today=only_today,
                          mode_filter=mode_filter, date_from=date_from, date_to=date_to)


@st.cache_data(ttl=60)
def _q_conviction_buckets(coin, days, only_today, mode_filter, date_from=None, date_to=None):
    return get_conviction_bucket_stats(coin=coin, days=days, only_today=only_today,
                                       mode_filter=mode_filter, date_from=date_from, date_to=date_to)


@st.cache_data(ttl=60)
def _q_regime_buckets(coin, days, only_today, mode_filter, date_from=None, date_to=None):
    return get_regime_bucket_stats(coin=coin, days=days, only_today=only_today,
                                   mode_filter=mode_filter, date_from=date_from, date_to=date_to)


@st.cache_data(ttl=60)
def _q_ofi_buckets(coin, days, only_today, mode_filter, date_from=None, date_to=None):
    return get_ofi_bucket_stats(coin=coin, days=days, only_today=only_today,
                                mode_filter=mode_filter, date_from=date_from, date_to=date_to)


@st.cache_data(ttl=60)
def _q_conviction_sweep(coin, days, only_today, mode_filter, date_from=None, date_to=None):
    return get_conviction_threshold_sweep(coin=coin, days=days, only_today=only_today,
                                          mode_filter=mode_filter, date_from=date_from, date_to=date_to)


@st.cache_data(ttl=60)
def _q_pnl_by_exit_reason(coin, days, only_today):
    return get_pnl_by_exit_reason(coin=coin, days=days, only_today=only_today)


@st.cache_data(ttl=60)
def _q_mode_comparison(days, only_today):
    return get_mode_comparison(days=days, only_today=only_today)


@st.cache_data(ttl=120)
def _run_backtest_engine(coin, days):
    """Cache BacktestEngine resultaten 2 minuten — niet aanroepen zonder explicit verzoek."""
    engine = BacktestEngine(coin=coin if coin != "All" else None, days=days)
    if not engine.trades:
        return None
    return {
        "conviction": engine.sweep_conviction(),
        "regime": engine.sweep_regime(),
        "exit_reason": engine.sweep_exit_reason(),
        "grid": engine.grid_search(),
        "early_loser": engine.simulate_early_loser_sell(),
        "weighting_engine_trades": [t for t in engine.trades],  # store for weighting tab
        "n_trades": len(engine.trades),
    }


def _results_to_df(results: list[BacktestResult]) -> pd.DataFrame:
    return pd.DataFrame([r.as_dict() for r in results])


# ── Hoofd-fragment ─────────────────────────────────────────────────────────────

_MODE_LABELS = ["Alle", "Straddle", "Signal", "Auto Router"]
_MODE_KEYS   = {"Alle": None, "Straddle": "straddle", "Signal": "signal", "Auto Router": "auto_router"}


@st.fragment(run_every=60)
def _analytics_live() -> None:
    """Auto-refreshes every 60s. Must be nested inside analytics_panel (@st.fragment)
    so the run_every auto-rerun doesn't interfere with the outer tab structure."""
    # ── Zoekfilter ────────────────────────────────────────────────────────────
    col_coin, col_mode, col_range = st.columns([1.5, 2.5, 2])
    with col_coin:
        selected_coin = st.selectbox("Coin", ["Alle"] + COINS, key="an_coin", label_visibility="collapsed")
        st.caption("Coin")
    with col_mode:
        selected_mode_label = st.radio(
            "Mode", _MODE_LABELS, horizontal=True, key="an_mode", label_visibility="collapsed"
        )
        st.caption("Mode")
    with col_range:
        selected_range = st.radio(
            "Periode", list(_RANGE_DAYS.keys()), horizontal=True, key="an_range",
            index=0, label_visibility="collapsed"
        )
        st.caption("Periode")

    # Aangepast datumbereik — verschijnt alleen als "Aangepast" geselecteerd
    date_from_sel = date_to_sel = None
    if selected_range == "Aangepast":
        col_van, col_tot = st.columns(2)
        with col_van:
            date_from_sel = st.date_input("Van", value=date.today() - timedelta(days=7),
                                          key="an_date_from", format="DD/MM/YYYY")
        with col_tot:
            date_to_sel = st.date_input("Tot", value=date.today(),
                                        key="an_date_to", format="DD/MM/YYYY")

    col_btn, col_clear = st.columns([2, 1])
    with col_btn:
        apply_filter = st.button("🔍 Toepassen", key="an_apply_filter", type="primary")
    with col_clear:
        if st.session_state.get("an_results"):
            if st.button("✕ Wis", key="an_clear_filter"):
                st.session_state.pop("an_results", None)
                st.session_state.pop("bt_cache", None)
                st.session_state.pop("wt_cache", None)
                st.rerun()

    if apply_filter:
        coin_filter_new  = selected_coin if selected_coin != "Alle" else None
        mode_filter_new  = _MODE_KEYS[selected_mode_label]
        range_val        = _RANGE_DAYS[selected_range]
        date_from_str    = str(date_from_sel) if date_from_sel else None
        date_to_str      = str(date_to_sel)   if date_to_sel   else None
        days_new         = None if range_val == "custom" else range_val
        trades_new = _q_trades(coin_filter_new, days_new, False, mode_filter_new,
                               date_from_str, date_to_str)
        st.session_state["an_results"] = {
            "coin": coin_filter_new,
            "days": days_new,
            "range_label": selected_range,
            "mode_filter": mode_filter_new,
            "mode_label": selected_mode_label,
            "date_from": date_from_str,
            "date_to": date_to_str,
            "trades": trades_new,
        }
        st.session_state.pop("bt_cache", None)
        st.session_state.pop("wt_cache", None)

    st.divider()

    # ── Data bepalen ──────────────────────────────────────────────────────────
    results = st.session_state.get("an_results")
    if results:
        coin_filter  = results["coin"]
        days         = results["days"]
        range_label  = results["range_label"]
        mode_filter  = results.get("mode_filter", "straddle")
        mode_label   = results.get("mode_label", "Straddle")
        date_from    = results.get("date_from")
        date_to      = results.get("date_to")
        trades       = results["trades"]
        only_today   = False
        is_filtered  = True
    else:
        coin_filter = None
        days        = 1
        range_label = "24 uur"
        mode_filter = "straddle"
        mode_label  = "Straddle"
        date_from   = date_to = None
        only_today  = False
        trades      = _q_trades(None, 1, False, "straddle")
        is_filtered = False

    df = pd.DataFrame(trades) if trades else pd.DataFrame()

    # ── Caption + export ──────────────────────────────────────────────────────
    col_lbl, col_export = st.columns([4, 1])
    with col_lbl:
        prefix      = "🔍" if is_filtered else "📊"
        coin_suffix = f" · {coin_filter}" if coin_filter else " · alle coins"
        if date_from and date_to:
            period_str = f"{date_from} → {date_to}"
        else:
            period_str = range_label
        st.caption(f"{prefix} **{len(trades)} trades** · {period_str}{coin_suffix} · {mode_label}")
    with col_export:
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

    # ── Lege staat ────────────────────────────────────────────────────────────
    if df.empty:
        st.info("Geen voltooide triggered trades in deze periode.")
        if not is_filtered:
            st.caption("Gebruik het filter hierboven om een andere periode of mode te bekijken.")
        return

    # ── Key Metrics ────────────────────────────────────────────────────────────
    _key_metrics(df)

    # ── Mode Overzicht (alleen als mode_filter=None = "Alle") ──────────────────
    if mode_filter is None:
        _mode_overview(days, only_today, date_from, date_to)

    # ── Exit Reason Breakdown ──────────────────────────────────────────────────
    _exit_breakdown(coin_filter, days, only_today, mode_filter, date_from, date_to)

    # ── Per-Coin Vergelijking ──────────────────────────────────────────────────
    if coin_filter is None:
        _coin_comparison(days, only_today, mode_filter, date_from, date_to)

    # ── Cumulatief P&L ─────────────────────────────────────────────────────────
    _cumulative_pnl(df)

    # ── Ingeklapte secties ─────────────────────────────────────────────────────
    with st.expander("📈 Trailing & Uurlijkse Analyse", expanded=False):
        _trailing_metrics(df)
        st.divider()
        _hourly_analysis(coin_filter, days, only_today, mode_filter, date_from, date_to)

    with st.expander("🚪 Exit Analyse (P&L per exit-reden)", expanded=False):
        _exit_loss_analysis(coin_filter, days, only_today)

    with st.expander("🧠 Signal Analytics", expanded=False):
        _signal_analytics(coin_filter, days, only_today, mode_filter, date_from, date_to)

    with st.expander("🔮 Scenario Schetser", expanded=False):
        _scenario_schetser(df)

    with st.expander("🤖 Claude Analyse", expanded=False):
        _claude_analysis_section(df, coin_filter, days)

    with st.expander(f"📋 Trade Geschiedenis ({len(df)} trades)", expanded=False):
        _trade_history_inner(df)


# ── Secties ────────────────────────────────────────────────────────────────────

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
    c[0].metric("Totaal trades", total)
    c[1].metric("Getriggerd", len(triggered))
    c[2].metric("Afgebroken", len(aborted))
    c[3].metric("Win Rate", f"{win_rate:.1f}%")
    c[4].metric("Net P&L", f"€{net_pnl:+.2f}")
    c[5].metric("Gem. P&L/trade", f"€{avg_pnl:+.4f}")


def _mode_overview(days: int | None, only_today: bool = False,
                   date_from: str | None = None, date_to: str | None = None) -> None:
    rows = _q_mode_comparison(days, only_today)  # mode_comparison altijd zonder date filter — overzicht
    if not rows or len(rows) < 2:
        return
    _MODE_DISPLAY = {
        "straddle":    "🔁 Straddle",
        "signal_trader": "📡 Signal",
        "auto_router": "🤖 Auto Router",
    }
    cols = st.columns(len(rows))
    for col, row in zip(cols, rows):
        label = _MODE_DISPLAY.get(row["trade_mode"], row["trade_mode"])
        pnl = row["total_pnl"] or 0.0
        delta = f"{row['win_pct'] or 0:.0f}% win · n={row['n']}"
        col.metric(label, f"€{pnl:+.2f}", delta)


def _exit_breakdown(coin: str | None, days: int | None, only_today: bool = False,
                    mode_filter: str | None = "straddle",
                    date_from: str | None = None, date_to: str | None = None) -> None:
    st.markdown("### Exit Reason Breakdown")
    stats = _q_exit_stats(coin, days, only_today, mode_filter, date_from, date_to)
    if not stats:
        st.caption("Geen data.")
        return

    sdf = pd.DataFrame(stats)
    total_trades = sdf["count"].sum()
    sdf.insert(2, "pct", (sdf["count"] / total_trades * 100).round(1).astype(str) + "%")

    col_tbl, col_chart = st.columns([3, 2])
    with col_tbl:
        display = sdf.rename(columns={
            "winner_exit_reason": "Exit Reden",
            "count": "Aantal", "pct": "%",
            "avg_pnl": "Gem. P&L", "total_pnl": "Totaal P&L",
            "avg_peak_bid": "Gem. Peak Bid",
            "avg_ratchets": "Gem. Ratchets",
            "avg_trail_time": "Gem. Trail (s)",
        })
        st.dataframe(display, use_container_width=True, hide_index=True)
    with col_chart:
        st.bar_chart(sdf.set_index("winner_exit_reason")[["count"]])


def _coin_comparison(days: int | None, only_today: bool = False,
                     mode_filter: str | None = "straddle",
                     date_from: str | None = None, date_to: str | None = None) -> None:
    st.markdown("### Per-Coin Vergelijking")
    rows = _q_coin_comparison(days, only_today, mode_filter, date_from, date_to)
    if not rows:
        st.caption("Geen data.")
        return

    cdf = pd.DataFrame(rows)
    cdf.insert(0, "", [COIN_EMOJI.get(c, "") for c in cdf["coin"]])
    cdf = cdf.rename(columns={
        "coin": "Coin", "trades": "Trades", "win_rate": "Win %",
        "total_pnl": "Net P&L", "avg_pnl": "Gem. P&L",
        "avg_peak_bid": "Gem. Peak Bid", "avg_trail_time": "Gem. Trail (s)",
        "avg_fees": "Gem. Fees",
    })
    st.dataframe(cdf, use_container_width=True, hide_index=True)


def _cumulative_pnl(df: pd.DataFrame) -> None:
    st.markdown("### Cumulatief P&L")
    plot_df = df[["created_at", "coin", "net_pnl"]].copy()
    plot_df["created_at"] = pd.to_datetime(plot_df["created_at"], format="mixed", utc=True)
    plot_df = plot_df.sort_values("created_at")

    chart = (
        plot_df.pivot_table(index="created_at", columns="coin", values="net_pnl", aggfunc="sum")
        .sort_index().fillna(0).cumsum()
    )
    chart.columns.name = None
    chart["Totaal"] = plot_df.groupby("created_at")["net_pnl"].sum().cumsum()
    st.line_chart(chart.ffill().fillna(0))


def _trailing_metrics(df: pd.DataFrame) -> None:
    triggered = df[df["trigger_hit"] == 1].copy() if "trigger_hit" in df.columns else df.copy()
    if triggered.empty:
        st.caption("Geen getriggerde trades in deze periode.")
        return

    st.markdown("**Trailing strategie metrics**")
    c = st.columns(4)
    avg_trail = triggered["time_in_trail_seconds"].mean() if "time_in_trail_seconds" in triggered else None
    avg_peak = triggered["peak_bid"].mean() if "peak_bid" in triggered else None
    avg_ratchet = triggered["ratchet_count"].mean() if "ratchet_count" in triggered else None
    avg_fees = triggered["fees_paid"].mean() if "fees_paid" in triggered else None

    c[0].metric("Gem. Trail Tijd", f"{avg_trail:.1f}s" if pd.notna(avg_trail) else "—")
    c[1].metric("Gem. Peak Bid", f"{float(avg_peak):.4f}" if pd.notna(avg_peak) else "—")
    c[2].metric("Gem. Ratchets", f"{avg_ratchet:.1f}" if pd.notna(avg_ratchet) else "—")
    c[3].metric("Gem. Fees", f"€{float(avg_fees):.4f}" if pd.notna(avg_fees) else "—")

    scatter = triggered[["time_in_trail_seconds", "net_pnl", "winner_exit_reason"]].dropna()
    if not scatter.empty:
        st.markdown("**Trail Tijd vs P&L**")
        st.scatter_chart(scatter, x="time_in_trail_seconds", y="net_pnl", color="winner_exit_reason")


def _hourly_analysis(coin: str | None, days: int | None, only_today: bool = False,
                     mode_filter: str | None = "straddle",
                     date_from: str | None = None, date_to: str | None = None) -> None:
    st.markdown("**Gem. P&L per uur van de dag (UTC)**")
    hourly = _q_hourly_pnl(coin, days, only_today, mode_filter, date_from, date_to)
    if not hourly:
        st.caption("Onvoldoende data (minimaal 100+ trades voor betrouwbaar signaal).")
        return
    hdf = pd.DataFrame(hourly).set_index("hour_utc")
    col_pnl, col_acc = st.columns(2)
    with col_pnl:
        st.caption("Gem. P&L per uur")
        st.bar_chart(hdf[["avg_pnl"]])
    with col_acc:
        st.caption("Richtingsnauwkeurigheid % per uur")
        acc = hdf[["direction_accuracy"]].dropna()
        if not acc.empty:
            st.bar_chart(acc)
        else:
            st.caption("Nog geen richtingsdata.")


def _exit_loss_analysis(coin: str | None, days: int | None, only_today: bool = False) -> None:
    st.caption(
        "P&L per exit-reden — laat zien welk kanaal het meeste verlies veroorzaakt. "
        "Rood = verliesgevend; groen = winstgevend. Gesorteerd van slechtste naar beste."
    )
    rows = _q_pnl_by_exit_reason(coin, days, only_today)
    if not rows:
        st.caption("Geen getriggerde + gesloten trades in deze periode.")
        return

    edf = pd.DataFrame(rows)
    rename_map = {
        "exit_reason": "Exit reden",
        "n": "Trades",
        "total_pnl": "Totaal P&L",
        "avg_pnl": "Gem. P&L",
        "avg_loss": "Gem. verlies",
        "total_loss": "Totaal verlies",
        "avg_exit_price": "Gem. exit-prijs",
    }
    display = edf.rename(columns=rename_map)

    def _color_pnl(val):
        if isinstance(val, (int, float)):
            return "color: #e05b5b" if val < 0 else "color: #4caf50"
        return ""

    pnl_cols = ["Totaal P&L", "Gem. P&L", "Gem. verlies", "Totaal verlies"]
    available_pnl_cols = [c for c in pnl_cols if c in display.columns]
    st.dataframe(
        display.style.map(_color_pnl, subset=available_pnl_cols),
        use_container_width=True, hide_index=True,
    )

    edf = edf.sort_values("total_pnl", na_position="last")
    chart_data = edf.set_index("exit_reason")[["total_pnl"]]
    st.bar_chart(chart_data, use_container_width=True)

    worst = edf.iloc[0]
    best = edf.iloc[-1]
    col_w, col_b = st.columns(2)
    with col_w:
        _pnl_w = float(worst["total_pnl"] or 0)
        st.metric(
            "Grootste verliesbron",
            worst["exit_reason"],
            f"€{_pnl_w:.2f} ({int(worst['n'] or 0)} trades)",
            delta_color="off",
        )
    with col_b:
        _pnl_b = float(best["total_pnl"] or 0)
        st.metric(
            "Grootste winstbron",
            best["exit_reason"],
            f"€{_pnl_b:.2f} ({int(best['n'] or 0)} trades)",
            delta_color="off",
        )


def _signal_analytics(coin: str | None, days: int | None, only_today: bool = False,
                      mode_filter: str | None = "straddle",
                      date_from: str | None = None, date_to: str | None = None) -> None:
    st.caption(
        "Win rate en P&L per signaalklasse — gebaseerd op getriggerde + gesloten trades. "
        "Gebruik dit om te bepalen welke signaalcombinaties daadwerkelijk een edge geven."
    )

    tab_conv, tab_regime, tab_ofi, tab_backtest, tab_weighting, tab_vroeg = st.tabs(
        ["Conviction", "Regime", "OFI", "Scenario Replay", "Gewogen inkoop", "Vroeg verkopen"]
    )

    with tab_conv:
        rows = _q_conviction_buckets(coin, days, only_today, mode_filter, date_from, date_to)
        if rows:
            bdf = pd.DataFrame(rows)
            st.dataframe(
                bdf.rename(columns={
                    "bucket": "Conviction bucket", "n": "Trades",
                    "win_pct": "Win %", "avg_net_pnl": "Gem. P&L", "total_pnl": "Totaal P&L",
                }),
                use_container_width=True, hide_index=True,
            )
            valid = bdf[bdf["bucket"] != "Geen signaal"]
            if not valid.empty:
                st.bar_chart(valid.set_index("bucket")[["win_pct"]])
        else:
            st.caption("Nog geen data (trades moeten getriggerd + gesloten zijn).")

    with tab_regime:
        rows = _q_regime_buckets(coin, days, only_today, mode_filter, date_from, date_to)
        if rows:
            rdf = pd.DataFrame(rows)
            st.dataframe(
                rdf.rename(columns={
                    "regime": "Regime", "n": "Trades",
                    "win_pct": "Win %", "avg_net_pnl": "Gem. P&L",
                    "total_pnl": "Totaal P&L", "peg_cross_pct": "PegCross %",
                }),
                use_container_width=True, hide_index=True,
            )
            st.bar_chart(rdf.set_index("regime")[["win_pct", "avg_net_pnl"]])
        else:
            st.caption("Nog geen data.")

    with tab_ofi:
        rows = _q_ofi_buckets(coin, days, only_today, mode_filter, date_from, date_to)
        if rows:
            odf = pd.DataFrame(rows)
            st.dataframe(
                odf.rename(columns={
                    "bucket": "OFI bucket", "n": "Trades",
                    "win_pct": "Win %", "avg_net_pnl": "Gem. P&L", "total_pnl": "Totaal P&L",
                }),
                use_container_width=True, hide_index=True,
            )
            valid = odf[odf["bucket"] != "Geen data"]
            if not valid.empty:
                st.bar_chart(valid.set_index("bucket")[["win_pct"]])
        else:
            st.caption("Nog geen data.")

    with tab_backtest:
        st.caption(
            "Replay van historische trades. Elke rij toont wat er was gebeurd als je "
            "alleen onder die conditie had gehandeld. Kan 1–10 seconden duren."
        )
        if st.button("▶️ Bereken Scenario Replay", key="bt_run"):
            with st.spinner("Scenario replay berekenen…"):
                bt_data = _run_backtest_engine(coin, days)
            st.session_state["bt_cache"] = {"coin": coin, "days": days, "data": bt_data}

        cached = st.session_state.get("bt_cache", {})
        bt = cached.get("data") if (cached.get("coin") == coin and cached.get("days") == days) else None

        if bt is None:
            st.caption("Klik '▶️ Bereken' om de scenario replay te starten.")
        else:
            st.caption(f"Gebaseerd op {bt['n_trades']} gesloten trades.")
            _render_backtest_results(bt, coin, days)

    with tab_weighting:
        st.markdown("**Gewogen inkoop simulatie**")
        st.caption(
            "Simuleert wat de historische P&L was geweest als de biased kant "
            "meer ingekocht was op basis van de conviction score. "
            "**Let op:** hogere max ratio vergroot ook verlies bij foute richting."
        )
        max_ratio = st.slider(
            "Max gewichtsverhouding (biased/neutraal)", 1.2, 3.0, 2.0, 0.1,
            key="sim_max_ratio",
        )
        if st.button("▶️ Bereken gewogen inkoop", key="wt_run"):
            with st.spinner("Simulatie berekenen…"):
                engine = BacktestEngine(coin=coin if coin != "All" else None, days=days)
                sim_rows = engine.simulate_weighting(max_ratio=max_ratio)
            st.session_state["wt_cache"] = {
                "coin": coin, "days": days, "rows": sim_rows, "ratio": max_ratio,
            }

        cached_wt = st.session_state.get("wt_cache", {})
        sim_rows = (
            cached_wt.get("rows")
            if (cached_wt.get("coin") == coin and cached_wt.get("days") == days)
            else None
        )

        if sim_rows is None:
            st.caption("Klik '▶️ Bereken' om de simulatie te starten.")
        else:
            _render_weighting_results(sim_rows, max_ratio)

    with tab_vroeg:
        cached = st.session_state.get("bt_cache", {})
        bt = cached.get("data") if (cached.get("coin") == coin and cached.get("days") == days) else None

        if bt is None:
            st.caption("Bereken eerst via de **Scenario Replay** tab — de vroeg-verkopen analyse deelt dezelfde berekening.")
        elif not bt.get("early_loser"):
            st.caption(
                "Geen data voor vroeg verkopen. Vereist gesloten triggered trades met "
                "snapshot-geschiedenis (snapshots elke 30s opgeslagen tijdens monitoring)."
            )
        else:
            _render_early_loser_results(bt["early_loser"])


def _render_backtest_results(bt: dict, coin: str | None, days: int | None) -> None:
    # 1. Conviction sweep
    st.markdown("**Conviction drempel**")
    cdf = _results_to_df(bt["conviction"])
    st.dataframe(cdf, use_container_width=True, hide_index=True)
    col_wl, col_pnl = st.columns(2)
    with col_wl:
        st.caption("Win % per drempel")
        st.line_chart(cdf.set_index("Strategie")[["Winrate %"]])
    with col_pnl:
        st.caption("Totaal P&L per drempel")
        st.line_chart(cdf.set_index("Strategie")[["Totaal P&L"]])

    st.divider()

    # 2. Regime sweep
    st.markdown("**Regime filter**")
    rdf = _results_to_df(bt["regime"])
    st.dataframe(rdf, use_container_width=True, hide_index=True)
    st.bar_chart(rdf.set_index("Strategie")[["Totaal P&L", "Winrate %"]])

    st.divider()

    # 3. Exit reason breakdown
    st.markdown("**Exit-reden analyse**")
    st.caption("Hoe presteren peg_cross, limit_filled en held_for_resolution apart?")
    edf = _results_to_df(bt["exit_reason"])
    st.dataframe(edf, use_container_width=True, hide_index=True)

    st.divider()

    # 4. Grid search
    st.markdown("**Grid search: conviction × regime** *(gesorteerd op P&L)*")
    gdf = _results_to_df(bt["grid"])
    st.dataframe(gdf, use_container_width=True, hide_index=True)

    # Equity curve: best vs baseline
    grid_results: list[BacktestResult] = bt["grid"]
    conv_results: list[BacktestResult] = bt["conviction"]
    baseline = next((r for r in conv_results if r.name == "≥0.00"), None)
    best = grid_results[0] if grid_results else None
    if best and baseline and best.cumulative_pnl and baseline.cumulative_pnl:
        st.divider()
        st.markdown("**Equity curve: baseline vs beste strategie**")
        n = min(len(best.cumulative_pnl), len(baseline.cumulative_pnl))
        curve_df = pd.DataFrame({
            "Baseline (alle trades)": baseline.cumulative_pnl[:n],
            f"Beste: {best.name}": best.cumulative_pnl[:n],
        })
        st.line_chart(curve_df)


def _render_weighting_results(sim_rows: list, max_ratio: float) -> None:
    sdf = pd.DataFrame(sim_rows)
    st.dataframe(sdf, use_container_width=True, hide_index=True)

    col_delta, col_dd = st.columns(2)
    with col_delta:
        st.caption("Delta P&L per min_score (positief = weging helpt)")
        st.bar_chart(sdf.set_index("Min score")[["Delta P&L"]])
    with col_dd:
        st.caption("Max drawdown (sim) per min_score")
        st.bar_chart(sdf.set_index("Min score")[["Max drawdown (sim)"]])

    best_row = max(sim_rows, key=lambda r: r["Delta P&L"])
    st.divider()
    st.markdown(f"**Beste drempel: min_score = {best_row['Min score']}**")
    col_a, col_b, col_c, col_d = st.columns(4)
    col_a.metric("Delta P&L", f"€{best_row['Delta P&L']:+.4f}")
    col_b.metric("Correct gewogen", best_row["Correct gewogen"])
    col_c.metric("Fout gewogen", best_row["Fout gewogen"])
    col_d.metric("Max drawdown (sim)", f"€{best_row['Max drawdown (sim)']:.4f}")

    cw_on = CONFIG.get("conviction_weighting", {}).get("enabled", False)
    if cw_on:
        st.success("Gewogen inkoop staat AAN (`conviction_weighting.enabled: true` in config.yaml)")
    else:
        st.info(
            "Gewogen inkoop staat UIT. Zet aan via `config.yaml`:\n\n"
            "```yaml\nconviction_weighting:\n  enabled: true\n"
            f"  min_score: {best_row['Min score']}\n  max_ratio: {max_ratio}\n```"
        )


def _render_early_loser_results(early_loser: list) -> None:
    st.markdown("**Vroeg verkopen simulatie**")
    st.caption(
        "Per drempel: wat was de P&L als we de loser hadden verkocht op het moment "
        "dat zijn mid-prijs onder die drempel zakte — in plaats van bij de trigger (~27¢)? "
        "Winner trail blijft ongewijzigd."
    )
    eldf = pd.DataFrame(early_loser)
    st.dataframe(eldf, use_container_width=True, hide_index=True)

    if not eldf.empty and "Delta P&L" in eldf.columns:
        col_delta, col_price = st.columns(2)
        with col_delta:
            st.caption("Delta P&L per drempel (positief = vroeg verkopen helpt)")
            st.bar_chart(eldf.set_index("Loser mid drempel")[["Delta P&L"]])
        with col_price:
            st.caption("Gem. loser exit prijs: vroeg vs. huidig")
            price_cols = [c for c in ["Gem. loser bid (vroeg)", "Gem. loser bid (huidig)"]
                          if c in eldf.columns]
            if price_cols:
                plot_df = eldf.set_index("Loser mid drempel")[price_cols].dropna()
                if not plot_df.empty:
                    st.line_chart(plot_df)

        best = max(early_loser, key=lambda r: r.get("Delta P&L", 0))
        if best["Delta P&L"] > 0:
            st.divider()
            st.markdown(f"**Beste drempel: loser mid ≤ {best['Loser mid drempel']}**")
            col_a, col_b, col_c, col_d = st.columns(4)
            col_a.metric("Delta P&L", f"€{best['Delta P&L']:+.4f}")
            col_b.metric("Gem. loser bid (vroeg)", f"{best.get('Gem. loser bid (vroeg)', 0):.3f}")
            col_c.metric("Gem. loser bid (huidig)", f"{best.get('Gem. loser bid (huidig)', 0):.3f}")
            col_d.metric("Trades vroeg exit", best["Trades vroeg exit"])
            st.info(
                f"Aanbevolen: verkoop de loser zodra zijn mid ≤ **{best['Loser mid drempel']}** "
                f"(ca. {best.get('Gem. loser bid (vroeg)', 0):.2f} bid)."
            )
        else:
            st.caption("Geen drempel verbetert de P&L — vroeg verkopen helpt hier niet.")


def _scenario_schetser(df: pd.DataFrame) -> None:
    """Simuleer trades als een andere modus en vergelijk met de werkelijkheid."""
    st.caption(
        "Overhevel de geselecteerde trades naar een andere strategie en zie wat het resultaat was geweest. "
        "Gebruikt de werkelijke exit-prijzen uit de database — geen aannames."
    )

    # Haal alleen gesloten + getriggerde trades op
    closed = df[df["status"].isin(["closed", "resolved"]) & (df.get("trigger_hit", pd.Series(dtype=int)) == 1)].copy() \
        if "trigger_hit" in df.columns else df[df["status"].isin(["closed", "resolved"])].copy()

    if closed.empty:
        st.info("Geen gesloten triggered trades in de huidige selectie.")
        return

    col_scen, col_run = st.columns([3, 1])
    with col_scen:
        scenario = st.radio(
            "Simuleer als",
            ["🔁 Straddle (beide kanten)", "📡 Signal Trader (conviction richting)"],
            horizontal=True, key="scen_type",
        )
    with col_run:
        run = st.button("▶️ Bereken", key="scen_run", type="primary")

    if not run and not st.session_state.get("scen_cache"):
        st.caption("Selecteer een scenario en klik op Bereken.")
        return

    if run:
        if "Straddle" in scenario:
            result = _sim_as_straddle(closed)
        else:
            result = _sim_as_signal(closed)
        st.session_state["scen_cache"] = result

    res = st.session_state.get("scen_cache")
    if not res:
        return

    # ── Resultaten ────────────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Trades gesimuleerd", res["n"])
    c2.metric("Actueel P&L", f"€{res['actual_total']:+.2f}")
    c3.metric("Gesimuleerd P&L", f"€{res['sim_total']:+.2f}")
    delta_color = "normal"
    c4.metric("Delta", f"€{res['delta']:+.2f}", delta=f"{res['delta']:+.2f}")

    win_col, _ = st.columns([1, 2])
    win_col.metric("Win rate (sim)", f"{res['win_pct']:.1f}%")

    if res.get("n_no_signal", 0) > 0:
        st.caption(f"⚠️ {res['n_no_signal']} trades zonder conviction-signaal overgeslagen.")

    # Compacte per-coin tabel
    if "df" in res and not res["df"].empty:
        sdf = res["df"]
        coin_summary = (
            sdf.groupby("coin")
            .agg(n=("sim", "count"), actueel=("actual", "sum"), gesimuleerd=("sim", "sum"))
            .round(2).reset_index()
        )
        coin_summary["delta"] = (coin_summary["gesimuleerd"] - coin_summary["actueel"]).round(2)
        coin_summary.columns = ["Coin", "Trades", "Actueel €", "Gesimuleerd €", "Delta €"]
        st.dataframe(coin_summary, use_container_width=True, hide_index=True)

        # Bar chart: actueel vs gesimuleerd per coin
        chart_df = coin_summary.set_index("Coin")[["Actueel €", "Gesimuleerd €"]]
        st.bar_chart(chart_df)


def _sim_as_straddle(df: pd.DataFrame) -> dict:
    """Simuleer alle trades alsof ze als straddle zijn uitgevoerd (beide kanten gekocht)."""
    rows = []
    for _, r in df.iterrows():
        yes_e = float(r.get("entry_yes_price") or 0.50)
        no_e  = float(r.get("entry_no_price")  or (1.0 - yes_e))
        winner = r.get("actual_winner") or r.get("winner_side")
        w_exit = float(r.get("winner_exit_price") or 0.85)
        l_exit = float(r.get("loser_exit_price")  or 0.12)
        size   = float(r.get("entry_size") or 2)

        if winner == "YES":
            w_e, l_e = yes_e, no_e
        else:
            w_e, l_e = no_e, yes_e

        sim_pnl = (w_exit - w_e) * size + (l_exit - l_e) * size
        rows.append({"coin": r.get("coin"), "actual": float(r.get("net_pnl") or 0), "sim": sim_pnl})

    sdf = pd.DataFrame(rows)
    return {
        "n": len(sdf),
        "actual_total": round(sdf["actual"].sum(), 4),
        "sim_total":    round(sdf["sim"].sum(), 4),
        "delta":        round(sdf["sim"].sum() - sdf["actual"].sum(), 4),
        "win_pct":      round((sdf["sim"] > 0).mean() * 100, 1),
        "df": sdf,
    }


def _sim_as_signal(df: pd.DataFrame) -> dict:
    """Simuleer alle trades alsof ze als signal trader zijn uitgevoerd (conviction richting)."""
    rows, skipped = [], 0
    for _, r in df.iterrows():
        conviction = r.get("conviction_at_trigger")
        if not conviction:
            skipped += 1
            continue
        actual_winner = r.get("actual_winner") or r.get("winner_side")
        predicted = "YES" if conviction == "UP" else "NO"
        correct   = (predicted == actual_winner)

        entry = float(r.get("entry_yes_price") if predicted == "YES" else r.get("entry_no_price") or 0.50)
        size  = float(r.get("entry_size") or 2)
        exit_p = float(r.get("winner_exit_price") if correct else r.get("loser_exit_price") or 0.12)

        rows.append({"coin": r.get("coin"), "actual": float(r.get("net_pnl") or 0),
                     "sim": (exit_p - entry) * size, "correct": correct})

    if not rows:
        return {"n": 0, "actual_total": 0, "sim_total": 0, "delta": 0,
                "win_pct": 0, "n_no_signal": skipped, "df": pd.DataFrame()}

    sdf = pd.DataFrame(rows)
    return {
        "n":            len(sdf),
        "actual_total": round(sdf["actual"].sum(), 4),
        "sim_total":    round(sdf["sim"].sum(), 4),
        "delta":        round(sdf["sim"].sum() - sdf["actual"].sum(), 4),
        "win_pct":      round(sdf["correct"].mean() * 100, 1),
        "n_no_signal":  skipped,
        "df": sdf,
    }


def _trade_history_inner(df: pd.DataFrame) -> None:
    display_cols = [
        "created_at", "coin", "status", "mode", "triggered_by",
        "entry_yes_price", "entry_no_price",
        "loser_exit_price", "winner_exit_price", "winner_exit_reason",
        "actual_winner", "peak_bid", "ratchet_count", "time_in_trail_seconds",
        "fees_paid", "net_pnl",
    ]
    available = [c for c in display_cols if c in df.columns]
    show = df[available].copy().sort_values("created_at", ascending=False).head(200)

    # Resultaat kolom — ✅/❌/⚠️ — gebaseerd op winner_exit_reason + status
    def _fmt_result(row) -> str:
        reason = row.get("winner_exit_reason", "") or ""
        status = row.get("status", "") or ""
        if reason == "resolution_won":
            return "✅ Won"
        if reason == "resolution_lost":
            return "❌ Lost"
        if "abort" in reason.lower() or status == "aborted":
            return "⚠️ Afgebr."
        if status in ("closed", "resolved"):
            return "⚠️ Afgebr."
        return "⏳ Open"

    if "winner_exit_reason" in show.columns or "status" in show.columns:
        show.insert(2, "Resultaat", show.apply(lambda r: _fmt_result(r.to_dict()), axis=1))

    if "mode" in show.columns:
        show["mode"] = show["mode"].fillna("straddle")

    if "net_pnl" in show.columns:
        show["net_pnl"] = show["net_pnl"].apply(
            lambda x: f"€{x:+.2f}" if pd.notna(x) else "—"
        )

    if "created_at" in show.columns:
        show["created_at"] = (
            pd.to_datetime(show["created_at"], format="mixed", utc=True)
            .dt.strftime("%m-%d %H:%M")
        )

    st.dataframe(show, use_container_width=True, hide_index=True)
    if len(df) > 200:
        st.caption(f"Toont 200 van de {len(df)} trades. Download via de CSV-knop voor het volledige overzicht.")


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
    if st.button("Analyseer met Claude", key="an_claude_btn"):
        try:
            with st.status("Claude analyseert…", expanded=True) as _s:
                _s.write("Trades ophalen en samenvatten…")
                params = _build_current_params()
                _s.write("Claude API aanroepen (kan 15–30s duren)…")
                result = analyze_trades_sync(df.to_dict("records"), params)
                _s.update(label="✅ Analyse klaar!", state="complete")
            st.session_state["manual_analysis"] = result
        except Exception as exc:
            st.error(f"Analyse mislukt: {exc}")

    result = st.session_state.get("manual_analysis")
    if not result:
        st.caption("Klik 'Analyseer met Claude' om een AI-analyse te starten op de huidige dataset.")
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


@st.fragment
def analytics_panel() -> None:
    """Outer wrapper (no run_every) so the inner 60s auto-rerun is isolated
    and doesn't cause DOM reconciliation issues with the surrounding tab structure."""
    _analytics_live()
