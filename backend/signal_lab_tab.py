"""Signal Lab tab — complete per-trade data exploration table.

Every trade on one row with all signals, entry data, trigger data, and outcome.
Features: column group toggles, coin/period/outcome filters, pagination (50/page),
CSV export with configurable row count.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from src.db_sync import get_signal_lab_trades, export_query_trades
from src.config_loader import CONFIG

COINS = list(CONFIG["coins"].keys())
COIN_EMOJI = {"BTC": "₿", "ETH": "Ξ", "SOL": "◎", "XRP": "✕", "DOGE": "Ð"}
_LOCAL_TZ = ZoneInfo("Europe/Amsterdam")
_PAGE_SIZE = 50

# ── Column groups definition ───────────────────────────────────────────────────
# Each group: list of (display_name, source_column_or_computed_key)
# Computed keys start with "__" and are filled in _enrich_row()

_GROUPS: dict[str, list[tuple[str, str]]] = {
    "🪪 Identiteit": [
        ("Coin", "coin"),
        ("Status", "status"),
        ("Mode", "mode"),
        ("Window start", "__window_start_local"),
        ("Window einde", "__window_end_local"),
        ("Getriggerd door", "triggered_by"),
        ("Fase", "phase"),
    ],
    "📥 Entry": [
        ("YES prijs", "entry_yes_price"),
        ("NO prijs", "entry_no_price"),
        ("Entry cost", "__entry_cost"),
        ("YES size", "yes_size"),
        ("NO size", "no_size"),
        ("Basis size", "entry_size"),
        ("Bias zekerheid", "bias_certainty"),
        ("Break-even", "break_even_price"),
    ],
    "📡 Signalen bij entry": [
        ("OFI (entry)", "ofi_at_entry"),
        ("Funding rate (entry)", "funding_rate_at_entry"),
        ("Liq proxy (entry)", "liq_proxy_at_entry"),
        ("Convictie (entry)", "conviction_at_entry"),
        ("Convictie score (entry)", "conviction_score_at_entry"),
    ],
    "🎯 Trigger data": [
        ("Mid bij trigger", "mid_at_trigger"),
        ("Spread bij trigger", "spread_at_trigger"),
        ("Velocity bij trigger", "mid_velocity_at_trigger"),
        ("YES diepte (trigger)", "yes_depth_at_trigger"),
        ("NO diepte (trigger)", "no_depth_at_trigger"),
        ("Seconden na window start", "time_since_window_start"),
        ("Winner zijde", "winner_side"),
    ],
    "📡 Signalen bij trigger": [
        ("OFI (trigger)", "ofi_at_trigger"),
        ("Funding rate (trigger)", "funding_rate_at_trigger"),
        ("Liq proxy (trigger)", "liq_proxy_at_trigger"),
        ("Convictie (trigger)", "conviction_at_trigger"),
        ("Convictie score (trigger)", "conviction_score_at_trigger"),
    ],
    "🔮 Voorspellingen": [
        ("Regime bij entry", "regime_at_entry"),
        ("Bias richting", "bias_direction_at_entry"),
        ("Bias zekerheid", "bias_certainty"),
        ("Convictie bij entry", "conviction_at_entry"),
        ("Convictie score entry", "conviction_score_at_entry"),
        ("Convictie bij trigger", "conviction_at_trigger"),
        ("Convictie score trigger", "conviction_score_at_trigger"),
        ("Bias voorspelling correct", "__bias_pred_correct"),
        ("Signaal voorspelling correct", "__signal_pred_correct"),
        ("Signaal uitleg", "__signal_vs_outcome"),
    ],
    "🏁 Uitkomst": [
        ("Getriggerd", "__triggered"),
        ("Werkelijke winnaar", "actual_winner"),
        ("Richting correct", "__direction_correct"),
        ("Exit reden", "winner_exit_reason"),
        ("Loser exit prijs", "loser_exit_price"),
        ("Winner exit prijs", "winner_exit_price"),
        ("Peak bid", "peak_bid"),
        ("Ratchets", "ratchet_count"),
        ("Trail tijd (s)", "time_in_trail_seconds"),
        ("Bruto P&L", "gross_pnl"),
        ("Kosten", "fees_paid"),
        ("Netto P&L", "net_pnl"),
    ],
}

# Default groups shown on load
_DEFAULT_GROUPS = {"🪪 Identiteit", "📥 Entry", "🔮 Voorspellingen", "🎯 Trigger data", "🏁 Uitkomst"}

_RANGE_DAYS: dict[str, int | None] = {"All time": None, "30 dagen": 30, "7 dagen": 7, "Vandaag": None}


# ── Row enrichment ─────────────────────────────────────────────────────────────

def _fmt_local(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_LOCAL_TZ).strftime("%d-%m %H:%M")
    except Exception:
        return iso[:16]


def _enrich(row: dict) -> dict:
    """Add computed columns (prefixed with __) to a raw DB row."""
    row = dict(row)
    row["__window_start_local"] = _fmt_local(row.get("window_start_ts"))
    row["__window_end_local"] = _fmt_local(row.get("window_end_ts"))
    yes = row.get("entry_yes_price")
    no = row.get("entry_no_price")
    row["__entry_cost"] = round(yes + no, 4) if (yes is not None and no is not None) else None
    row["__triggered"] = "Ja" if row.get("trigger_hit") else "Nee"
    winner = row.get("winner_side")
    actual = row.get("actual_winner")
    if winner and actual:
        row["__direction_correct"] = "✅" if winner == actual else "❌"
    else:
        row["__direction_correct"] = None

    # Bias prediction: bias_direction_at_entry=UP → predicted YES wins
    bias_dir = row.get("bias_direction_at_entry")
    if bias_dir and actual:
        bias_pred = "YES" if bias_dir == "UP" else "NO"
        row["__bias_pred_correct"] = "✅" if bias_pred == actual else "❌"
    else:
        row["__bias_pred_correct"] = "—"

    # Signal prediction: conviction_at_trigger direction → predicted YES/NO
    conv_trig = row.get("conviction_at_trigger")
    if conv_trig and actual:
        signal_pred = "YES" if conv_trig == "UP" else "NO"
        row["__signal_pred_correct"] = "✅" if signal_pred == actual else "❌"
        row["__signal_vs_outcome"] = (
            f"Signaal: {conv_trig} → {signal_pred} | Werkelijk: {actual}"
        )
    else:
        row["__signal_pred_correct"] = "—"
        row["__signal_vs_outcome"] = (
            f"Geen signaal | Werkelijk: {actual}" if actual else "—"
        )

    return row


# ── Build DataFrame from selected groups ──────────────────────────────────────

def _build_df(trades: list[dict], active_groups: set[str]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    enriched = [_enrich(t) for t in trades]
    cols_wanted: list[tuple[str, str]] = []
    for group in _GROUPS:  # preserve order
        if group in active_groups:
            cols_wanted.extend(_GROUPS[group])

    rows = []
    for t in enriched:
        row = {}
        for display, key in cols_wanted:
            val = t.get(key)
            # Format floats sensibly
            if isinstance(val, float):
                val = round(val, 4)
            row[display] = val
        rows.append(row)
    return pd.DataFrame(rows)


# ── Styling helpers ────────────────────────────────────────────────────────────

def _color_pnl(df: pd.DataFrame) -> pd.io.formats.style.Styler:
    """Colour Netto P&L column green/red."""
    styler = df.style
    if "Netto P&L" in df.columns:
        def _bg(v):
            try:
                f = float(v)
                return "background-color: rgba(52,211,153,0.15)" if f > 0 else (
                    "background-color: rgba(248,113,113,0.15)" if f < 0 else "")
            except Exception:
                return ""
        styler = styler.applymap(_bg, subset=["Netto P&L"])
    return styler


def _export_query(
    coin: str | None,
    date_start: str | None,
    date_end: str | None,
    triggered_only: bool,
    outcome: str | None,
    limit: int,
) -> tuple[list[dict], int]:
    """Wrapper that maps date_start/end to db_sync export_query_trades."""
    return export_query_trades(
        coin=coin,
        date_start=date_start,
        date_end=date_end,
        triggered_only=triggered_only,
        outcome=outcome,
        limit=limit,
    )


# ── Main panel ─────────────────────────────────────────────────────────────────

@st.fragment
def signal_lab_panel() -> None:
    st.markdown("### 🔬 Signal Lab")
    st.caption(
        "Alle trades op één rij — Binance signalen, Polymarket data, en uitkomst. "
        "Selecteer kolomgroepen, filter, pagineer en exporteer als CSV."
    )

    # ── Filters ───────────────────────────────────────────────────────────────
    f1, f2, f3, f4 = st.columns([1.5, 1.5, 1.5, 1.5])
    coin_options = ["Alle coins"] + COINS
    coin_sel = f1.selectbox("Coin", coin_options, key="slab_coin")
    range_sel = f2.selectbox("Periode", list(_RANGE_DAYS.keys()), key="slab_range")
    outcome_options = {"Alles": None, "Winst": "WIN", "Verlies": "LOSS", "Geen trigger": "NO_TRIGGER"}
    outcome_label = f3.selectbox("Uitkomst", list(outcome_options.keys()), key="slab_outcome")
    triggered_only = f4.checkbox("Alleen getriggerd", value=False, key="slab_trig_only")

    coin_filter = None if coin_sel == "Alle coins" else coin_sel
    outcome_filter = outcome_options[outcome_label]
    only_today = (range_sel == "Vandaag")
    days_filter = _RANGE_DAYS[range_sel] if not only_today else None

    # Reset page when filters change
    filter_key = (coin_filter, range_sel, outcome_label, triggered_only)
    if st.session_state.get("slab_filter_key") != filter_key:
        st.session_state["slab_filter_key"] = filter_key
        st.session_state["slab_page"] = 0

    # ── Column group toggles ──────────────────────────────────────────────────
    with st.expander("Kolomgroepen", expanded=False):
        gcols = st.columns(len(_GROUPS))
        active_groups: set[str] = set()
        for i, group_name in enumerate(_GROUPS):
            default_on = group_name in _DEFAULT_GROUPS
            if gcols[i].checkbox(group_name, value=default_on, key=f"slab_grp_{i}"):
                active_groups.add(group_name)

    if not active_groups:
        st.info("Selecteer minstens één kolomgroep.")
        return

    # ── Query ─────────────────────────────────────────────────────────────────
    page = st.session_state.get("slab_page", 0)
    offset = page * _PAGE_SIZE

    trades, total = get_signal_lab_trades(
        coin=coin_filter,
        days=days_filter,
        only_today=only_today,
        triggered_only=triggered_only,
        outcome=outcome_filter,
        limit=_PAGE_SIZE,
        offset=offset,
    )
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    # ── Summary metrics ───────────────────────────────────────────────────────
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Trades (totaal)", total)

    triggered_count = sum(1 for t in trades if t.get("trigger_hit"))
    m2.metric("Getriggerd", triggered_count, delta=f"/{len(trades)} op pagina")

    wins = [t for t in trades if t.get("trigger_hit") and (t.get("net_pnl") or 0) > 0]
    losses = [t for t in trades if t.get("trigger_hit") and (t.get("net_pnl") or 0) <= 0]
    trig_page = len(wins) + len(losses)
    wr = round(len(wins) / trig_page * 100, 1) if trig_page else None
    m3.metric("Winrate (pagina)", f"{wr}%" if wr is not None else "—")

    pnl_vals = [t.get("net_pnl") for t in trades if t.get("net_pnl") is not None]
    total_pnl = round(sum(pnl_vals), 4) if pnl_vals else None
    m4.metric("Netto P&L (pagina)", f"€{total_pnl:+.4f}" if total_pnl is not None else "—")

    # Signal prediction accuracy (conviction_at_trigger vs actual_winner)
    sig_correct = 0; sig_total = 0
    bias_correct = 0; bias_total = 0
    for t in trades:
        actual = t.get("actual_winner")
        if not actual:
            continue
        conv = t.get("conviction_at_trigger")
        if conv:
            sig_total += 1
            if (conv == "UP" and actual == "YES") or (conv == "DOWN" and actual == "NO"):
                sig_correct += 1
        bias = t.get("bias_direction_at_entry")
        if bias:
            bias_total += 1
            expected = "YES" if bias == "UP" else "NO"
            if expected == actual:
                bias_correct += 1

    sig_acc = round(sig_correct / sig_total * 100, 1) if sig_total else None
    m5.metric("Signaal acc. (trigger)", f"{sig_acc}%" if sig_acc is not None else "—",
              help=f"Hoe vaak klopt conviction_at_trigger met werkelijke winnaar ({sig_total} meetpunten)")

    bias_acc = round(bias_correct / bias_total * 100, 1) if bias_total else None
    m6.metric("Bias acc. (entry)", f"{bias_acc}%" if bias_acc is not None else "—",
              help=f"Hoe vaak klopt bias_direction_at_entry met werkelijke winnaar ({bias_total} meetpunten)")

    # ── Table ─────────────────────────────────────────────────────────────────
    df = _build_df(trades, active_groups)
    if df.empty:
        st.info("Geen trades gevonden voor deze filters.")
    else:
        try:
            styled = _color_pnl(df)
            st.dataframe(styled, use_container_width=True, hide_index=True, height=520)
        except Exception:
            st.dataframe(df, use_container_width=True, hide_index=True, height=520)

    # ── Pagination ────────────────────────────────────────────────────────────
    st.markdown(f"<p style='color:#6b7280;font-size:12px'>Pagina {page+1} van {total_pages} — {total} trades totaal</p>",
                unsafe_allow_html=True)
    pc1, pc2, pc3 = st.columns([1, 3, 1])
    if pc1.button("◀ Vorige", disabled=(page == 0), key="slab_prev"):
        st.session_state["slab_page"] = max(0, page - 1)
        st.rerun()
    pc2.empty()
    if pc3.button("Volgende ▶", disabled=(page >= total_pages - 1), key="slab_next"):
        st.session_state["slab_page"] = page + 1
        st.rerun()

    # ── Quick download — same filters as the table, all pages ─────────────────
    st.divider()
    qd1, qd2 = st.columns([3, 1])
    qd1.markdown(
        f"<span style='color:#6b7280;font-size:13px'>⬇ Download alle <b>{total}</b> trades die aan de bovenstaande filters voldoen (geen paginering)</span>",
        unsafe_allow_html=True,
    )
    if qd2.button("📥 Download CSV", key="slab_quick_dl", type="secondary"):
        with st.spinner(f"Alle {total} trades ophalen..."):
            all_trades, _ = export_query_trades(
                coin=coin_filter,
                date_start=None,
                date_end=None,
                triggered_only=triggered_only,
                outcome=outcome_filter,
                limit=10_000,
                days=days_filter,
                only_today=only_today,
            )
        dl_df = _build_df(all_trades, active_groups)
        if not dl_df.empty:
            buf = io.StringIO()
            dl_df.to_csv(buf, index=False)
            ts = datetime.now().strftime("%Y%m%d_%H%M")
            st.download_button(
                label=f"⬇ Sla {len(dl_df)} rijen op",
                data=buf.getvalue().encode("utf-8"),
                file_name=f"signal_lab_{coin_filter or 'all'}_{ts}.csv",
                mime="text/csv",
                key="slab_quick_dl_btn",
            )

    # ── Export ────────────────────────────────────────────────────────────────
    with st.expander("📤 Geavanceerde export — eigen filters en kolomselectie", expanded=False):
        st.caption(
            "⚠️ Export haalt **rechtstreeks uit de database**, onafhankelijk van de tabel hierboven. "
            "Je kunt andere filters en kolommen kiezen. De paginering (50/pagina) geldt NIET voor exports."
        )

        # Export-specifieke filters
        ecol1, ecol2, ecol3, ecol4 = st.columns(4)
        exp_coin_options = ["Alle coins"] + COINS
        exp_coin = ecol1.selectbox("Coin (export)", exp_coin_options, key="exp_coin")
        exp_coin_filter = None if exp_coin == "Alle coins" else exp_coin

        exp_start = ecol2.date_input(
            "Startdatum", value=None, key="exp_start",
            help="Leeg = geen ondergrens"
        )
        exp_end = ecol3.date_input(
            "Einddatum", value=None, key="exp_end",
            help="Leeg = tot vandaag"
        )
        exp_outcome_lbl = ecol4.selectbox(
            "Uitkomst (export)",
            ["Alles", "Winst", "Verlies", "Geen trigger"],
            key="exp_outcome",
        )
        exp_outcome_map = {"Alles": None, "Winst": "WIN", "Verlies": "LOSS", "Geen trigger": "NO_TRIGGER"}
        exp_outcome_filter = exp_outcome_map[exp_outcome_lbl]

        exp_triggered = st.checkbox("Alleen getriggerd (export)", value=False, key="exp_trig")
        exp_max_rows = st.number_input(
            "Max rijen", min_value=10, max_value=10_000, value=1000, step=100, key="exp_max_rows"
        )

        # Kolomselectie
        st.markdown("**Kolommen voor export:**")
        # Raw DB columns + computed columns per group
        all_group_names = list(_GROUPS.keys())
        exp_groups = st.multiselect(
            "Kolomgroepen",
            options=all_group_names,
            default=all_group_names,
            key="exp_groups",
            help="Vink groepen uit om kolommen die leeg zijn voor oudere trades te verwijderen",
        )

        # Show null-column warning for groups added after the project start
        _new_groups = {"📡 Signalen bij entry", "📡 Signalen bij trigger", "🔮 Voorspellingen"}
        new_selected = _new_groups & set(exp_groups)
        if new_selected:
            st.info(
                f"Let op: {', '.join(sorted(new_selected))} bevatten NULL-waarden voor trades "
                "van vóór de Phase 1 update. Zet ze uit voor een schonere export van oudere data."
            )

        if st.button("🔄 Genereer export", key="slab_gen_export", type="primary"):
            # Build date-range conditions
            exp_days = None
            exp_only_today = False
            if exp_start or exp_end:
                # Use raw SQL via a custom query — we'll pass a custom date filter
                st.session_state["exp_date_start"] = str(exp_start) if exp_start else None
                st.session_state["exp_date_end"] = str(exp_end) if exp_end else None
            else:
                st.session_state["exp_date_start"] = None
                st.session_state["exp_date_end"] = None

            with st.spinner(f"Max {exp_max_rows} rijen ophalen..."):
                export_trades, export_total = _export_query(
                    coin=exp_coin_filter,
                    date_start=st.session_state.get("exp_date_start"),
                    date_end=st.session_state.get("exp_date_end"),
                    triggered_only=exp_triggered,
                    outcome=exp_outcome_filter,
                    limit=int(exp_max_rows),
                )

            if exp_groups:
                export_df = _build_df(export_trades, set(exp_groups))
            else:
                export_df = pd.DataFrame(export_trades)

            if not export_df.empty:
                buf = io.StringIO()
                export_df.to_csv(buf, index=False)
                csv_bytes = buf.getvalue().encode("utf-8")
                ts = datetime.now().strftime("%Y%m%d_%H%M")
                filename = f"signal_lab_{exp_coin_filter or 'all'}_{ts}.csv"
                st.success(f"✅ {len(export_df)} rijen klaar (van {export_total} totaal met deze filters)")
                st.download_button(
                    label=f"⬇ Download {len(export_df)} rijen als CSV",
                    data=csv_bytes,
                    file_name=filename,
                    mime="text/csv",
                    key="slab_download",
                )
            else:
                st.warning("Geen trades gevonden met deze exportfilters.")

    # ── Pattern hints ─────────────────────────────────────────────────────────
    if trades and "📡 Signalen bij trigger" in active_groups and triggered_count > 5:
        with st.expander("📊 Patroon hints (getriggerde trades op pagina)", expanded=False):
            _pattern_hints(trades)


def _pattern_hints(trades: list[dict]) -> None:
    """Quick correlation hints between signals and trade outcome."""
    trig = [t for t in trades if t.get("trigger_hit") and t.get("net_pnl") is not None]
    if len(trig) < 3:
        st.caption("Te weinig data voor hints.")
        return

    wins = [t for t in trig if t.get("net_pnl", 0) > 0]
    losses = [t for t in trig if t.get("net_pnl", 0) <= 0]

    def _avg(lst, key):
        vals = [v for t in lst if (v := t.get(key)) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    rows = []
    for label, w_key in [
        ("OFI bij trigger", "ofi_at_trigger"),
        ("OFI bij entry", "ofi_at_entry"),
        ("Convictie score (trigger)", "conviction_score_at_trigger"),
        ("Convictie score (entry)", "conviction_score_at_entry"),
        ("Funding rate (trigger)", "funding_rate_at_trigger"),
        ("Liq proxy (trigger)", "liq_proxy_at_trigger"),
        ("Mid bij trigger", "mid_at_trigger"),
        ("Spread bij trigger", "spread_at_trigger"),
        ("Seconden na window start", "time_since_window_start"),
    ]:
        w_avg = _avg(wins, w_key)
        l_avg = _avg(losses, w_key)
        if w_avg is not None or l_avg is not None:
            rows.append({
                "Signaal": label,
                f"Gem. winst ({len(wins)}x)": w_avg,
                f"Gem. verlies ({len(losses)}x)": l_avg,
                "Verschil": round(w_avg - l_avg, 3) if (w_avg is not None and l_avg is not None) else None,
            })

    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption(
            "Let op: correlatie ≠ causaliteit. Gebruik dit als startpunt voor fase 2 (backtest)."
        )
    else:
        st.caption("Geen signaaldata beschikbaar voor vergelijking.")
