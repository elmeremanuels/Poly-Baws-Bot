"""Analytics tab for the Poly-Baws-Bot Streamlit dashboard."""
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
_RANGE_DAYS = {"24 uur": 1, "7 dagen": 7, "30 dagen": 30, "Alle tijd": None}


# ── Gecachede DB-queries ───────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def _q_trades(coin, days, only_today):
    return get_analytics_trades(coin=coin, days=days, only_today=only_today)


@st.cache_data(ttl=60)
def _q_exit_stats(coin, days, only_today):
    return get_exit_reason_stats(coin=coin, days=days, only_today=only_today)


@st.cache_data(ttl=60)
def _q_coin_comparison(days, only_today):
    return get_coin_comparison(days=days, only_today=only_today)


@st.cache_data(ttl=60)
def _q_hourly_pnl(coin, days, only_today):
    return get_hourly_pnl(coin=coin, days=days, only_today=only_today)


@st.cache_data(ttl=60)
def _q_conviction_buckets(coin, days, only_today):
    return get_conviction_bucket_stats(coin=coin, days=days, only_today=only_today)


@st.cache_data(ttl=60)
def _q_regime_buckets(coin, days, only_today):
    return get_regime_bucket_stats(coin=coin, days=days, only_today=only_today)


@st.cache_data(ttl=60)
def _q_ofi_buckets(coin, days, only_today):
    return get_ofi_bucket_stats(coin=coin, days=days, only_today=only_today)


@st.cache_data(ttl=60)
def _q_conviction_sweep(coin, days, only_today):
    return get_conviction_threshold_sweep(coin=coin, days=days, only_today=only_today)


@st.cache_data(ttl=60)
def _q_pnl_by_exit_reason(coin, days, only_today):
    return get_pnl_by_exit_reason(coin=coin, days=days, only_today=only_today)


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

@st.fragment
def analytics_panel() -> None:
    # ── Zoekfilter ────────────────────────────────────────────────────────────
    st.markdown("### 🔍 Zoekfilter")
    col_coin, col_range = st.columns([2, 3])
    with col_coin:
        selected_coin = st.selectbox("Coin", ["Alle"] + COINS, key="an_coin")
    with col_range:
        selected_range = st.radio(
            "Periode", list(_RANGE_DAYS.keys()), horizontal=True, key="an_range", index=0
        )

    col_btn, col_clear = st.columns([2, 1])
    with col_btn:
        apply_filter = st.button("🔍 Zoekfilter toepassen", key="an_apply_filter", type="primary")
    with col_clear:
        if st.session_state.get("an_results"):
            if st.button("✕ Wis filter", key="an_clear_filter"):
                st.session_state.pop("an_results", None)
                st.session_state.pop("bt_cache", None)
                st.session_state.pop("wt_cache", None)
                st.rerun()

    if apply_filter:
        coin_filter_new = selected_coin if selected_coin != "Alle" else None
        days_new = _RANGE_DAYS[selected_range]
        trades_new = _q_trades(coin_filter_new, days_new, False)
        st.session_state["an_results"] = {
            "coin": coin_filter_new,
            "days": days_new,
            "range_label": selected_range,
            "trades": trades_new,
        }
        # Backtest cache ongeldig bij nieuwe filterparameters
        st.session_state.pop("bt_cache", None)
        st.session_state.pop("wt_cache", None)

    st.divider()

    # ── Data bepalen ──────────────────────────────────────────────────────────
    results = st.session_state.get("an_results")
    if results:
        coin_filter = results["coin"]
        days = results["days"]
        range_label = results["range_label"]
        trades = results["trades"]
        only_today = False
        is_filtered = True
    else:
        # Standaard: afgelopen 24 uur, alle coins — lichtgewicht
        coin_filter = None
        days = 1
        range_label = "24 uur"
        only_today = False
        trades = _q_trades(None, 1, False)
        is_filtered = False

    df = pd.DataFrame(trades) if trades else pd.DataFrame()

    # ── Label + export ────────────────────────────────────────────────────────
    col_lbl, col_export = st.columns([4, 1])
    with col_lbl:
        prefix = "🔍 Gefilterd" if is_filtered else "📊 Standaard (24 uur)"
        coin_suffix = f" — {coin_filter}" if coin_filter else " — alle coins"
        st.caption(f"{prefix}: **{len(trades)} trades** — {range_label}{coin_suffix}")
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
            st.caption("Gebruik het zoekfilter hierboven om een andere periode te bekijken.")
        return

    # ── Key Metrics — altijd prominent ────────────────────────────────────────
    _key_metrics(df)

    # ── Exit Reason Breakdown — altijd prominent ───────────────────────────────
    _exit_breakdown(coin_filter, days, only_today)

    # ── Per-Coin Vergelijking ─────────────────────────────────────────────────
    if coin_filter is None:
        _coin_comparison(days, only_today)

    # ── Cumulatief P&L ────────────────────────────────────────────────────────
    _cumulative_pnl(df)

    # ── Ingeklapte secties (opt-in) ───────────────────────────────────────────
    with st.expander("📈 Trailing & Uurlijkse Analyse", expanded=False):
        _trailing_metrics(df)
        st.divider()
        _hourly_analysis(coin_filter, days, only_today)

    with st.expander("🚪 Exit Analyse (P&L per exit-reden)", expanded=False):
        _exit_loss_analysis(coin_filter, days, only_today)

    with st.expander("🧠 Signal Analytics", expanded=False):
        _signal_analytics(coin_filter, days, only_today)

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


def _exit_breakdown(coin: str | None, days: int | None, only_today: bool = False) -> None:
    st.markdown("### Exit Reason Breakdown")
    stats = _q_exit_stats(coin, days, only_today)
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


def _coin_comparison(days: int | None, only_today: bool = False) -> None:
    st.markdown("### Per-Coin Vergelijking")
    rows = _q_coin_comparison(days, only_today)
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


def _hourly_analysis(coin: str | None, days: int | None, only_today: bool = False) -> None:
    st.markdown("**Gem. P&L per uur van de dag (UTC)**")
    hourly = _q_hourly_pnl(coin, days, only_today)
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
        display.style.applymap(_color_pnl, subset=available_pnl_cols),
        use_container_width=True, hide_index=True,
    )

    chart_data = edf.set_index("exit_reason")[["total_pnl"]].sort_values("total_pnl")
    st.bar_chart(chart_data, use_container_width=True)

    worst = edf.iloc[0]
    best = edf.iloc[-1]
    col_w, col_b = st.columns(2)
    with col_w:
        st.metric(
            "Grootste verliesbron",
            worst["exit_reason"],
            f"€{worst['total_pnl']:.2f} ({int(worst['n'])} trades)",
            delta_color="off",
        )
    with col_b:
        st.metric(
            "Grootste winstbron",
            best["exit_reason"],
            f"€{best['total_pnl']:.2f} ({int(best['n'])} trades)",
            delta_color="off",
        )


def _signal_analytics(coin: str | None, days: int | None, only_today: bool = False) -> None:
    st.caption(
        "Win rate en P&L per signaalklasse — gebaseerd op getriggerde + gesloten trades. "
        "Gebruik dit om te bepalen welke signaalcombinaties daadwerkelijk een edge geven."
    )

    tab_conv, tab_regime, tab_ofi, tab_backtest, tab_weighting, tab_vroeg = st.tabs(
        ["Conviction", "Regime", "OFI", "Scenario Replay", "Gewogen inkoop", "Vroeg verkopen"]
    )

    with tab_conv:
        rows = _q_conviction_buckets(coin, days, only_today)
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
        rows = _q_regime_buckets(coin, days, only_today)
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
        rows = _q_ofi_buckets(coin, days, only_today)
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
