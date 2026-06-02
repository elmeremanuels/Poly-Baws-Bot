"""BGGDSB Tab — Beter Goed Gejat Dan Slecht Bedacht

Strategie 1:1 gebaseerd op is5minfixedyet (data-analyse 2026-05-30):
  1. Market competitive gate: beide kanten 0.10–0.90 (anders al bijna resolved)
  2. Richting: conviction signal (OFI + funding rate) → dominant kant
  3. Dominant kant: mediaan 8 tranches (~elke 14s), geen prijsgate na eerste entry
  4. Hedge: koop tegenovergestelde kant LAAT zodra prijs ≤ 0.15 (mediaan 0.11)
  5. Totaal budget: €20–50 per window (instelbaar)
  6. Exit: hold to expiry (€1.00) — geen vroegtijdige exit
  7. Optioneel: is5 live activiteit als extra bevestigingssignaal
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import streamlit as st
import pandas as pd

from src.db_sync import (
    get_state,
    set_dashboard_state,
    get_bggdsb_stats,
    get_bggdsb_trades,
    get_bggdsb_coin_scoreboard,
    is5_live_status,
)
from src.commands import write_command
from src.config_loader import CONFIG as _CFG
from src.risk import KILL_FLAG_PATH as _KILL_FLAG_PATH, get_kill_reason as _get_kill_reason

_IS5_ADDRESS = "0x2bc01f3ad80e31f5bf3d80775b044f0c67797871"
_ALL_COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]

# ── Cached data ────────────────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def _q_is5_live() -> dict:
    return is5_live_status()

@st.cache_data(ttl=60)
def _q_stats(mode_filter: str | None = None) -> dict:
    return get_bggdsb_stats(mode_filter=mode_filter)

@st.cache_data(ttl=30)
def _q_trades(limit: int = 100, mode_filter: str | None = None) -> list[dict]:
    return get_bggdsb_trades(limit=limit, mode_filter=mode_filter)

@st.cache_data(ttl=20)
def _q_scoreboard() -> dict:
    return get_bggdsb_coin_scoreboard(_ALL_COINS, lookback=15)


# Regime → emoji + korte uitleg (RANGING is best geschikt voor de hold-strategie)
_REGIME_META = {
    "RANGING":  ("🟦 Ranging",  "zijwaarts — beste fit"),
    "TRENDING": ("🟩 Trending", "trend — hedge weinig waard"),
    "BREAKOUT": ("🟧 Breakout", "uitbraak — richting cruciaal"),
    "CHOPPY":   ("🟥 Choppy",   "grillig — signaal onbetrouwbaar"),
    "NORMAL":   ("⬜ Normal",   "neutraal"),
    "UNKNOWN":  ("⬛ Unknown",  "nog te weinig data"),
}


# Per-regime minimale geschiktheidsscore om GROEN (= instapbaar) te tonen.
# Onder de drempel: oranje (dichtbij) of rood. Choppy nooit groen, ongeacht score.
# Unknown/te weinig data: grijs (geen oordeel).
_REGIME_GREEN_MIN: dict[str, int | None] = {
    "RANGING":  65,    # ideaal 70%+
    "TRENDING": 72,    # ideaal 78%+
    "BREAKOUT": 75,    # ideaal 80%+ — voorzichtig
    "NORMAL":   72,    # neutraal → behandel als trending
    "CHOPPY":   None,  # nooit instappen
    "UNKNOWN":  None,  # te weinig regime-data
}
_MIN_N_FOR_GREEN = 5   # < 5 trades → score onbetrouwbaar, nooit groen


def _suitability_color(pct: float | None, regime: str = "UNKNOWN", n: int = 0) -> str:
    if pct is None:
        return "rgba(150,150,150,0.6)"   # grijs — geen data
    if regime == "CHOPPY":
        return "#ef5350"                 # rood — nooit instappen, ongeacht score
    green_min = _REGIME_GREEN_MIN.get(regime)
    if green_min is None or n < _MIN_N_FOR_GREEN:
        return "rgba(150,150,150,0.6)"   # grijs — onbetrouwbaar / te weinig data
    if pct >= green_min:
        return "#00c853"                 # groen — instapbaar volgens tabel
    if pct >= green_min - 12:
        return "#ff9800"                 # oranje — dichtbij drempel
    return "#ef5350"                     # rood — te laag


def _render_coin_suitability(coin: str, board: dict, current_mode: str,
                             active_coins: list[str]) -> None:
    """Compacte geschiktheids-weergave onder een coin-checkbox: % + regime + status."""
    info = board.get(coin, {}) or {}
    pct = info.get("suitability_pct")
    n = info.get("n", 0)
    pct_txt = f"{pct:.0f}%" if pct is not None else "—"

    # Regime label uit dashboard_state (door bot gezet in _regime_sync_loop)
    reg = "UNKNOWN"
    reg_label, reg_help = "⬛ Unknown", ""
    raw_reg = get_state(f"regime_{coin}")
    if raw_reg:
        try:
            reg = (json.loads(raw_reg) or {}).get("regime", "UNKNOWN")
            reg_label, reg_help = _REGIME_META.get(reg, (reg, ""))
        except Exception:
            pass

    # Kleur is regime-bewust: alleen groen als score ≥ drempel voor dit regime
    # én n ≥ 5 (zie tabel). Choppy nooit groen, Unknown/te weinig data → grijs.
    color = _suitability_color(pct, reg, n)
    _gmin = _REGIME_GREEN_MIN.get(reg)
    _thr_txt = f"instap-drempel {_gmin}%" if _gmin is not None else "geen instap (regime)"

    if coin in active_coins:
        status = "🔴 live" if current_mode == "bggdsb_live" else "🟡 actief"
    else:
        status = "👁 schaduw"
    tip = f"{reg_help} · {_thr_txt} · recency-gewogen win% over laatste {n} trades"
    st.markdown(
        f"<div title='{tip}' style='line-height:1.25;margin-top:-4px'>"
        f"<span style='font-size:1.4em;font-weight:bold;color:{color}'>{pct_txt}</span>"
        f"<div style='font-size:0.72em'>{reg_label}</div>"
        f"<div style='font-size:0.66em;color:rgba(120,120,120,0.9)'>{status} · n={n}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )


def _load_selected_coins() -> list[str]:
    raw = get_state("bggdsb_coins")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [c for c in parsed if c in _ALL_COINS]
        except Exception:
            pass
    return ["BTC"]


def _bot_status() -> tuple[bool, str]:
    """Geeft (online, leeftijd_tekst) op basis van heartbeat."""
    hb = get_state("heartbeat")
    if not hb:
        return False, "nooit"
    try:
        ts = datetime.fromisoformat(hb.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age < 15:
            return True, f"{int(age)}s geleden"
        if age < 60:
            return False, f"{int(age)}s geleden"
        return False, f"{int(age//60)}min geleden"
    except Exception:
        return False, "onbekend"


# ── Live section (nested inner fragment) ──────────────────────────────────────


def _build_card_html(win: dict) -> str:
    """Returns HTML for one compact trade card (flexbox-safe, no st.columns)."""
    coin_w  = win.get("coin", "?")
    phase   = win.get("phase", "monitoring")
    dom     = win.get("dominant_side", "?")
    yes_sp  = float(win.get("yes_spend", 0))
    no_sp   = float(win.get("no_spend", 0))
    tot_sp  = yes_sp + no_sp
    yes_mid = float(win.get("yes_mid", 0))
    no_mid  = float(win.get("no_mid", 0))
    secs    = int(win.get("secs_left", 0))
    yes_sh  = float(win.get("yes_shares", 0))
    no_sh   = float(win.get("no_shares", 0))

    other_side = "NO" if dom == "YES" else "YES"
    other_sh   = no_sh  if other_side == "NO" else yes_sh
    be_ok      = tot_sp > 0 and other_sh >= tot_sp
    be_need    = max(0.0, round((tot_sp - other_sh) * max(
        (no_mid if other_side == "NO" else yes_mid), 0.01), 2))

    current_value = yes_sh * yes_mid + no_sh * no_mid
    virtual_pnl   = round(current_value - tot_sp, 2) if tot_sp > 0 else 0.0

    pnl_color = "#ff9800" if abs(virtual_pnl) < 1.0 else ("#00c853" if virtual_pnl > 0 else "#ef5350")

    phase_icon = {"monitoring": "🔍", "flipping": "🔄", "confirmed": "✅", "done": "✔"}.get(phase, "⏳")
    phase_txt  = {"monitoring": "Monitoring", "flipping": "Flipping",
                  "confirmed": "Break-even", "done": "Done"}.get(phase, phase)

    winner     = "YES" if yes_mid >= no_mid else "NO"
    winner_mid = max(yes_mid, no_mid)
    if be_ok:
        status_color = "#00c853"
        status_txt   = "✅ Break-even bereikt"
    elif phase == "flipping":
        status_color = "#ff9800"
        status_txt   = f"🔄 Flipping → {other_side} (nog €{be_need:.2f})"
    elif winner_mid >= 0.55:
        status_color = "#448aff"
        status_txt   = f"📈 {winner} dominant ({winner_mid:.2f})"
    else:
        status_color = "rgba(120,120,120,0.8)"
        status_txt   = "⏳ Onbeslist"

    mins = secs // 60
    secs_rem = secs % 60
    time_txt = f"{mins}:{secs_rem:02d}"

    return (
        f"<div style='flex:1 1 28%;min-width:200px;border:1px solid rgba(120,120,120,0.25);"
        f"border-radius:10px;padding:12px 14px;line-height:1.45;background:rgba(0,0,0,0.05)'>"
        f"<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px'>"
        f"<span style='font-size:1.15em;font-weight:700'>{coin_w}</span>"
        f"<span style='font-size:0.8em;color:rgba(160,160,160,0.9)'>{phase_icon} {phase_txt}</span>"
        f"</div>"
        f"<div style='font-size:1.5em;font-weight:bold;color:{pnl_color};margin-bottom:4px'>"
        f"€{virtual_pnl:+.2f}</div>"
        f"<div style='display:grid;grid-template-columns:1fr 1fr;gap:2px 10px;font-size:0.8em;margin-bottom:6px'>"
        f"<span>YES <b>{yes_mid:.3f}</b> · {yes_sh:.1f}sh · €{yes_sp:.2f}</span>"
        f"<span>NO <b>{no_mid:.3f}</b> · {no_sh:.1f}sh · €{no_sp:.2f}</span>"
        f"<span>💶 Totaal <b>€{tot_sp:.2f}</b></span>"
        f"<span>⏱ <b>{time_txt}</b></span>"
        f"</div>"
        f"<div style='font-size:0.78em;color:{status_color};font-weight:500'>{status_txt}</div>"
        f"</div>"
    )


@st.fragment(run_every=5)
def _bggdsb_live_section() -> None:
    """Bot/is5 status + active trade cards — auto-refreshes every 5s.

    Kept as a nested inner fragment so the outer bggdsb_panel does NOT need
    run_every, which would conflict with Streamlit's tab reconciliation when
    the toggle changes the number of visible tabs.
    """
    import json as _json

    # Bot / is5 / mode status
    bot_online, bot_age = _bot_status()
    status = _q_is5_live()
    live = status.get("live", False)
    n_recent = status.get("trades_recent", 0)
    last_seen = status.get("last_seen", "onbekend")

    bot_col, is5_col, mode_col = st.columns(3)
    with bot_col:
        if bot_online:
            st.success(f"🟢 **Bot** — heartbeat {bot_age}", icon="🤖")
        else:
            st.error(f"🔴 **Bot offline** — {bot_age}", icon="🤖")
    with is5_col:
        if live:
            st.success(f"🟢 **is5** LIVE — {n_recent} trades", icon="📡")
        else:
            st.info(f"⚫ **is5** offline — {last_seen}", icon="📡")
    with mode_col:
        _cur_mode = get_state("mode") or "onbekend"
        _bggdsb_on = _cur_mode in ("bggdsb_paper", "bggdsb_live")
        if _bggdsb_on:
            _label = "🟡 Paper" if _cur_mode == "bggdsb_paper" else "💸 Live"
            st.success(f"**BGGDSB** — {_label}", icon="🧠")
        else:
            st.warning(f"Modus: `{_cur_mode}`", icon="⚠️")

    # Active trade cards
    try:
        _wins_raw = get_state("bggdsb_active_windows") or "{}"
        _all_wins = _json.loads(_wins_raw) if _wins_raw else {}
    except Exception:
        _all_wins = {}
    active_wins = [w for w in _all_wins.values() if w.get("secs_left", 0) > 0]

    # Sound alert for new windows in live mode
    if _cur_mode == "bggdsb_live":
        _cur_keys = {w.get("window_key", "") for w in active_wins}
        _prev_keys = set(st.session_state.get("bggdsb_seen_window_keys") or [])
        _new_keys = _cur_keys - _prev_keys
        if _new_keys:
            st.session_state["bggdsb_seen_window_keys"] = list(_cur_keys)
            st.components.v1.html(
                """<script>
(function(){
  try {
    var ctx = new (window.AudioContext || window.webkitAudioContext)();
    var osc = ctx.createOscillator();
    var gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.type = 'sine';
    osc.frequency.setValueAtTime(880, ctx.currentTime);
    gain.gain.setValueAtTime(0.25, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + 0.3);
  } catch(e) {}
})();
</script>""",
                height=0,
            )

    if active_wins:
        _cards_inner = "".join(_build_card_html(w) for w in active_wins)
    else:
        _coins_label = ", ".join(_load_selected_coins()) or "munten"
        _cards_inner = (
            f"<span style='color:rgba(150,150,150,0.8);font-size:0.9em'>"
            f"⏳ Geen actief window — bot zoekt volgende {_coins_label} window (~elke 5 min)</span>"
        )
    st.markdown(
        f"<div style='display:flex;flex-wrap:wrap;gap:12px;margin-bottom:8px'>{_cards_inner}</div>",
        unsafe_allow_html=True,
    )


# ── Main panel ────────────────────────────────────────────────────────────────

@st.fragment  # no run_every — inner _bggdsb_live_section handles 5s auto-refresh
def bggdsb_panel() -> None:
    st.subheader("🧠 BGGDSB")
    st.caption("*Beter Goed Gejat Dan Slecht Bedacht* — is5minfixedyet strategie 1:1")

    _bggdsb_live_section()

    st.divider()

    current_mode = get_state("mode") or "onbekend"
    bggdsb_active = current_mode in ("bggdsb_paper", "bggdsb_live")

    # ── Mode filter voor stats + tabel ───────────────────────────────────────
    _mode_options = {"💸 Alleen live": "bggdsb_live", "🟡 Alleen paper": "bggdsb_paper", "📊 Alle": None}
    _default_mode_label = "💸 Alleen live" if current_mode == "bggdsb_live" else "🟡 Alleen paper" if current_mode == "bggdsb_paper" else "📊 Alle"
    # Set session state default only once — don't override after user picks something.
    # Passing index= on every rerun would reset the selection each time the fragment reruns.
    if "bggdsb_mode_filter" not in st.session_state:
        st.session_state["bggdsb_mode_filter"] = _default_mode_label
    _selected_mode_label = st.radio(
        "Toon trades van", list(_mode_options.keys()),
        horizontal=True, key="bggdsb_mode_filter",
    )
    _mode_filter = _mode_options[_selected_mode_label]

    # ── Performance metrics ───────────────────────────────────────────────────
    stats = _q_stats(mode_filter=_mode_filter)
    if stats and stats.get("n", 0) > 0:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Trades", stats["n"])
        m2.metric("Win%", f"{stats.get('win_pct', 0):.1f}%")
        m3.metric("Totaal P&L", f"€{stats.get('total_pnl', 0):+.2f}")
        m4.metric("Gem. P&L", f"€{stats.get('avg_pnl', 0):+.4f}")
    else:
        st.caption("📊 Nog geen afgeronde BGGDSB trades — metrics verschijnen na het eerste window.")

    # ── Streak status ─────────────────────────────────────────────────────────
    streak_wins = int(get_state("bggdsb_streak_wins") or 0)
    streak_skip = int(get_state("bggdsb_streak_skip") or 0)
    if streak_skip > 0:
        st.warning(
            f"⏸ **Streak onderbroken** — {streak_skip} window(s) overgeslagen "
            f"(streak herstel na verlies na {streak_wins + (streak_skip)} wins op rij)",
            icon="🛑",
        )
    elif streak_wins >= 2:
        st.success(f"🔥 **Winning streak: {streak_wins}** wins op rij", icon="🔥")
    elif streak_wins == 1:
        st.caption("✅ 1 win op rij")

    # ── Laatste skip-reden (reasoning box) ───────────────────────────────────
    last_skip = get_state("bggdsb_last_skip_reason") or ""
    if last_skip:
        st.info(f"**Laatste skip:** {last_skip}", icon="ℹ️")

    st.divider()

    # ── Recente trades ────────────────────────────────────────────────────────
    st.markdown("**Recente trades**")
    trades = _q_trades(mode_filter=_mode_filter)
    if not trades:
        st.caption("Geen BGGDSB trades gevonden.")
    else:
        df = pd.DataFrame(trades)
        n_aborted    = int((df["status"] == "aborted").sum())    if "status" in df.columns else 0
        n_monitoring = int((df["status"] == "monitoring").sum()) if "status" in df.columns else 0
        # Only show completed trades — monitoring rows have no winner/P&L yet.
        if "status" in df.columns:
            df = df[df["status"].isin(["closed", "resolved"])].copy()

        # Afleiding: instaprichting uit yes_size/no_size
        def _dir(row) -> str:
            ys = row.get("yes_size") or 0
            return "Up" if float(ys) > 0 else "Down"

        def _correct(row) -> str:
            winner = row.get("winner_side")
            if not winner:
                return ""
            entry_is_yes = float(row.get("yes_size") or 0) > 0
            won = (entry_is_yes and winner == "YES") or (not entry_is_yes and winner == "NO")
            return "✅" if won else "❌"

        df["Richting"]  = df.apply(_dir, axis=1)
        df["Juist"]     = df.apply(_correct, axis=1)

        # YES/NO → Up/Down voor Winnaar
        df["winner_side"] = df["winner_side"].map({"YES": "Up", "NO": "Down"}).fillna("")

        # Instap prijs = whichever side was entered
        df["entry_price"] = df.apply(
            lambda r: r.get("entry_yes_price") if float(r.get("yes_size") or 0) > 0
                      else r.get("entry_no_price"),
            axis=1,
        )
        # Aandelen = dom side shares
        df["entry_shares"] = df.apply(
            lambda r: r.get("yes_size") if float(r.get("yes_size") or 0) > 0
                      else r.get("no_size"),
            axis=1,
        )

        cols = [c for c in [
            "created_at", "coin", "Richting", "entry_price", "entry_shares",
            "winner_side", "Juist", "winner_exit_reason", "net_pnl", "status"
        ] if c in df.columns]
        df_s = df[cols].copy()
        df_s.columns = [
            {"created_at": "Tijd", "coin": "Coin", "entry_price": "Instap prijs",
             "entry_shares": "Aandelen", "winner_side": "Winnaar",
             "winner_exit_reason": "Exit reden", "net_pnl": "P&L",
             "status": "Status"}.get(c, c)
            for c in cols
        ]
        if "Instap prijs" in df_s.columns:
            df_s["Instap prijs"] = df_s["Instap prijs"].apply(
                lambda x: f"{float(x):.3f}" if pd.notna(x) and x is not None else ""
            )
        if "Aandelen" in df_s.columns:
            df_s["Aandelen"] = df_s["Aandelen"].apply(
                lambda x: f"{float(x):.2f}" if pd.notna(x) and x is not None else ""
            )
        if "P&L" in df_s.columns:
            df_s["P&L"] = df_s["P&L"].apply(
                lambda x: f"€{float(x):+.2f}" if pd.notna(x) and x is not None else ""
            )
        st.dataframe(df_s, hide_index=True, use_container_width=True)
        n_correct = int((df["Juist"] == "✅").sum())
        n_decided = int((df["Juist"].isin(["✅", "❌"])).sum())
        caption_parts = [f"{len(df_s)} trades"]
        if n_decided:
            caption_parts.append(f"richting {n_correct}/{n_decided} juist ({n_correct/n_decided*100:.0f}%)")
        if n_aborted:
            caption_parts.append(f"{n_aborted} instap-pogingen verborgen")
        if n_monitoring:
            caption_parts.append(f"{n_monitoring} in uitvoering (verborgen)")
        st.caption(" · ".join(caption_parts))

    st.divider()

    # ── Controls ──────────────────────────────────────────────────────────────
    if not bggdsb_active:
        st.info(
            f"Instellingen zijn uitgeschakeld — bot staat in `{current_mode}` modus. "
            "Selecteer **🧠 BGGDSB paper** of **🔴 BGGDSB live** in de sidebar om te bewerken.",
            icon="🔒",
        )
    col_a, col_c, col_d = st.columns([3, 3, 1])

    with col_a:
        _saved_budget = int(get_state("bggdsb_window_budget") or 2)
        budget = st.slider(
            "💶 Budget per window (€)",
            min_value=2, max_value=200, step=1,
            value=_saved_budget,
            key="bggdsb_budget",
            disabled=not bggdsb_active,
            help=None if bggdsb_active else "Activeer eerst BGGDSB modus via de sidebar.",
        )
        _bc = _CFG.get("bggdsb", {})
        _avg_pct     = float(_bc.get("avg_down_tranche_pct", 0.50))
        _avg_max_pct = float(_bc.get("avg_down_max_pct", 4.00))
        _flip_pct    = float(_bc.get("flip_tranche_pct", 0.75))
        _flip_max_pct= float(_bc.get("flip_max_pct", 3.00))
        _conf_pct    = float(_bc.get("confirm_pct", 1.00))
        _hedge_pct   = float(_bc.get("hedge_size_pct", 0.10))
        _hedge_trig  = float(_bc.get("hedge_price_trigger", 0.11))
        _flip_trig   = float(_bc.get("flip_trigger_price", 0.62))
        _conf_trig   = float(_bc.get("confirm_trigger_price", 0.78))
        _conf_secs   = int(_bc.get("confirm_secs_remaining", 90))

        entry_t      = round(budget * 1.00, 2)
        avg_down_t   = round(budget * _avg_pct, 2)
        avg_down_max = round(budget * _avg_max_pct, 2)
        flip_t       = round(budget * _flip_pct, 2)
        flip_max     = round(budget * _flip_max_pct, 2)
        confirm_t    = round(budget * _conf_pct, 2)
        hedge_t      = round(budget * _hedge_pct, 2)
        max_possible = round(budget + avg_down_max + flip_max + confirm_t + hedge_t, 2)

        st.caption("**Bedragen bij dit budget (uit config):**")
        bc1, bc2, bc3, bc4, bc5 = st.columns(5)
        bc1.metric("🟢 Entry", f"€{entry_t:.2f}",
                   help="Volledige inleg op dominante kant bij window-start")
        bc2.metric("📉 Avg-down", f"€{avg_down_t:.2f}", f"max €{avg_down_max:.2f}",
                   help=f"Per tranche bijkopen als dom. kant > {_bc.get('avg_down_min_drop', 0.08):.0%} daalt "
                        f"({_avg_pct:.0%} budget/tranche, max {_avg_max_pct:.0f}× budget)")
        bc3.metric("🔄 Flip", f"€{flip_t:.2f}", f"max €{flip_max:.2f}",
                   help=f"Per tranche andere kant kopen als die > {_flip_trig} stijgt "
                        f"({_flip_pct:.0%} budget/tranche, max {_flip_max_pct:.0f}× budget)")
        bc4.metric("✅ Confirm", f"€{confirm_t:.2f}",
                   help=f"Eenmalige extra koop in laatste {_conf_secs}s als winnaar ≥ {_conf_trig}")
        bc5.metric("🛡 Hedge", f"€{hedge_t:.2f}",
                   help=f"Verliezende kant kopen als prijs ≤ {_hedge_trig} ({_hedge_pct:.0%} van budget)")
        st.caption(f"⚠️ Max totaal per window zonder limiet: **€{max_possible:.2f}**")

    with col_c:
        is5_weight = st.slider(
            "📡 is5 signaalgewicht (%)",
            min_value=0, max_value=100, step=10,
            value=int(get_state("bggdsb_is5_signal_weight") or 0),
            key="bggdsb_is5_weight",
            disabled=not bggdsb_active,
            help=(
                "Activeer eerst BGGDSB modus via de sidebar." if not bggdsb_active else
                "0% = is5 nooit gebruikt (puur marktprijs). "
                "6% ≈ origineel (tiebreaker bij prijsverschil ≤ 3ct). "
                "50% = is5 tiebreaker tot 25ct verschil. "
                "100% = is5 altijd boven marktprijs."
            ),
        )

    with col_d:
        st.markdown("&nbsp;", unsafe_allow_html=True)  # vertical align

    # Coins row — checkbox + geschiktheid (recency-gewogen win%) per munt.
    # Elke munt draait op de achtergrond mee in paper (schaduw) — ook munten waar
    # we live niet in zitten. Groen ≥70% · oranje 40–69% · rood <40%.
    st.markdown("**Coins** — geschiktheid: 🟢 ≥70% · 🟠 40–69% · 🔴 <40%")
    saved_coins = _load_selected_coins()
    _board = _q_scoreboard()
    coin_cols = st.columns(len(_ALL_COINS))
    selected_coins = []
    for i, coin in enumerate(_ALL_COINS):
        with coin_cols[i]:
            checked = st.checkbox(
                coin,
                value=(coin in saved_coins),
                key=f"bggdsb_coin_{coin}",
                disabled=not bggdsb_active,
                help="is5 handelde vrijwel alleen BTC" if coin == "BTC" else None,
            )
            if checked:
                selected_coins.append(coin)
            _render_coin_suitability(coin, _board, current_mode, saved_coins)
            # Stop-bij-verlies: gepauzeerd na een verlies → toon hervat-knop
            if get_state(f"bggdsb_halted_{coin}") == "1":
                st.markdown(
                    "<div style='color:#ef5350;font-size:0.72em;font-weight:bold'>"
                    "⏸ gepauzeerd na verlies</div>",
                    unsafe_allow_html=True,
                )
                if st.button("▶ Hervat", key=f"bggdsb_resume_{coin}"):
                    set_dashboard_state(f"bggdsb_halted_{coin}", "0")
                    st.rerun()

    if not selected_coins:
        st.warning("Selecteer minstens één coin.", icon="⚠️")
    st.caption(
        "Schaduw-munten draaien continu paper mee zodat je ziet welke markt nu "
        "het best bij de strategie past — ook als je live niet in die munt zit."
    )

    # Stop-bij-verlies toggle: na een verlies gaat die munt niet de volgende
    # markt in. Wint 'ie, dan gewoon door. Per munt, handmatig hervatten.
    _stop_on_loss = st.checkbox(
        "🛑 Stop bij verlies — pauzeer een munt na elk verlies (per munt)",
        value=(get_state("bggdsb_stop_on_loss") == "1"),
        key="bggdsb_stop_on_loss_cb",
        disabled=not bggdsb_active,
        help="Aan: verliest een munt een window, dan stopt die munt met nieuwe "
             "entries tot je 'Hervat' klikt. Winst → gewoon door. Schaduw-trades "
             "lopen altijd door. Uit: munten traden continu door.",
    )

    # Reverse entry experiment: koop de tegenkant van wat de markt dominant vindt.
    # Als de bot 90% van de time de verkeerde kant kiest, is de inverse winstgevend.
    _reverse_entry = st.checkbox(
        "🔄 Reverse initial buy — koop de tegenkant (experimenteel)",
        value=(get_state("bggdsb_reverse_entry") == "1"),
        key="bggdsb_reverse_cb",
        disabled=not bggdsb_active,
        help="Normaal koopt de bot de dominante kant (bijv. YES 0.62). "
             "Reverse koopt de onderkant (bijv. NO 0.38). "
             "Test of de inverse beslissing winstgevend is.",
    )
    if _reverse_entry:
        st.warning("🔄 **Reverse mode aan** — bot koopt de ONDERKANT van elke markt.", icon="🔄")
        # Advies-signaal: wanneer is reverse instappen zinvol?
        _rev_score_raw = get_state("bggdsb_reverse_entry_advice")
        _bggdsb_stats_all = _q_stats()
        _n_rev = int(_bggdsb_stats_all.get("total", 0))
        _win_rev = float(_bggdsb_stats_all.get("win_pct") or 0)
        if _n_rev >= 10:
            _inv_win = round(100 - _win_rev, 1)
            if _inv_win >= 65:
                st.success(
                    f"✅ **Goed moment voor reverse** — normaal win%: {_win_rev:.0f}% "
                    f"→ inverse verwacht {_inv_win:.0f}% (n={_n_rev})",
                    icon="📈",
                )
            elif _inv_win >= 50:
                st.info(
                    f"⚪ **Twijfelachtig** — normaal win%: {_win_rev:.0f}% "
                    f"→ inverse verwacht {_inv_win:.0f}% (breakevenzone, n={_n_rev})",
                    icon="📊",
                )
            else:
                st.error(
                    f"❌ **Niet aanbevolen** — normaal win%: {_win_rev:.0f}% "
                    f"→ inverse zou slechts {_inv_win:.0f}% halen (n={_n_rev})",
                    icon="📉",
                )
        else:
            st.caption(f"📊 Te weinig data voor advies (n={_n_rev}, minimum 10 trades nodig)")

    # Current live status
    _is_live_now = get_state("bggdsb_paper_mode") == "0"

    btn_col, live_col = st.columns([2, 3])
    with btn_col:
        if st.button("💾 Instellingen opslaan (paper)", key="bggdsb_save",
                     disabled=not selected_coins or not bggdsb_active):
            set_dashboard_state("bggdsb_window_budget", str(budget))
            set_dashboard_state("bggdsb_is5_signal_weight", str(is5_weight))
            set_dashboard_state("bggdsb_paper_mode", "1")   # altijd paper na opslaan
            set_dashboard_state("bggdsb_coins", json.dumps(selected_coins))
            set_dashboard_state("bggdsb_stop_on_loss", "1" if _stop_on_loss else "0")
            set_dashboard_state("bggdsb_reverse_entry", "1" if _reverse_entry else "0")
            for _c in _ALL_COINS:                            # verse start: hef pauzes op
                set_dashboard_state(f"bggdsb_halted_{_c}", "0")
            write_command("set_mode", {"mode": "bggdsb_paper"})
            coins_str = ", ".join(selected_coins)
            st.success(
                f"Opgeslagen in **paper** mode — budget €{budget}/window · coins: {coins_str}"
            )
            _q_is5_live.clear()
            _q_stats.clear()

    with live_col:
        if _is_live_now:
            if st.button("⏸ Terug naar paper", key="bggdsb_to_paper", type="secondary"):
                set_dashboard_state("bggdsb_paper_mode", "1")
                write_command("set_mode", {"mode": "bggdsb_paper"})
                # Sync sidebar radio so it doesn't send a phantom set_mode on next full rerun
                st.session_state["sidebar_mode_radio"] = "bggdsb_paper"
                st.warning("Teruggeschakeld naar **paper** mode.")
        else:
            if _KILL_FLAG_PATH.exists():
                _reason = _get_kill_reason()
                if _reason:
                    st.error(f"⛔ Bot gestopt: `{_reason}` — klik Resume om te hervatten")
            st.warning(
                f"⚠️ LIVE modus gebruikt **echt geld** — €{budget} per window. "
                "Zeker weten?",
                icon="🔴",
            )
            if st.button(
                f"🔴 Ga LIVE (€{budget}/window, echt geld!)",
                key="bggdsb_go_live",
                type="primary",
                disabled=not selected_coins,
            ):
                set_dashboard_state("bggdsb_window_budget", str(budget))
                set_dashboard_state("bggdsb_is5_signal_weight", str(is5_weight))
                set_dashboard_state("bggdsb_paper_mode", "0")
                set_dashboard_state("bggdsb_coins", json.dumps(selected_coins))
                set_dashboard_state("bggdsb_stop_on_loss", "1" if _stop_on_loss else "0")
                set_dashboard_state("bggdsb_reverse_entry", "1" if _reverse_entry else "0")
                for _c in _ALL_COINS:                        # verse start: hef pauzes op
                    set_dashboard_state(f"bggdsb_halted_{_c}", "0")
                # Delete kill flag immediately (Streamlit can access the file directly)
                # AND queue the command for the bot's in-memory _killed flag.
                if _KILL_FLAG_PATH.exists():
                    _KILL_FLAG_PATH.unlink()
                write_command("reset_kill")
                write_command("set_mode", {"mode": "bggdsb_live"})
                # Sync sidebar radio so it doesn't send a phantom set_mode on next full rerun
                st.session_state["sidebar_mode_radio"] = "bggdsb_live"
                st.toast("🔴 LIVE geactiveerd — wacht op eerste window", icon="🔴")
                st.error(
                    f"🔴 LIVE actief — €{budget}/window op {', '.join(selected_coins)}. "
                    "Klik 'Terug naar paper' om te stoppen."
                )

    st.divider()

    # ── Documentatie (expanders) ──────────────────────────────────────────────
    with st.expander("📐 Rekenvoorbeeld — is5minfixedyet strategie"):
        _bc2 = _CFG.get("bggdsb", {})
        dom_price       = 0.50
        dom_shares      = round(budget / dom_price, 1)
        avg_down_ex     = round(budget * float(_bc2.get("avg_down_tranche_pct", 0.50)), 2)
        avg_down_max2   = round(budget * float(_bc2.get("avg_down_max_pct", 4.00)), 2)
        flip_tranche_ex = round(budget * float(_bc2.get("flip_tranche_pct", 0.75)), 2)
        flip_max_ex     = round(budget * float(_bc2.get("flip_max_pct", 3.00)), 2)
        confirm_ex      = round(budget * float(_bc2.get("confirm_pct", 1.00)), 2)
        hedge_eur_ex    = round(budget * float(_bc2.get("hedge_size_pct", 0.10)), 2)
        flip_trig2      = float(_bc2.get("flip_trigger_price", 0.62))
        flip_int2       = int(_bc2.get("flip_interval_secs", 20))
        conf_trig2      = float(_bc2.get("confirm_trigger_price", 0.78))
        conf_secs2      = int(_bc2.get("confirm_secs_remaining", 90))
        hedge_trig2     = float(_bc2.get("hedge_price_trigger", 0.11))
        avg_drop2       = float(_bc2.get("avg_down_min_drop", 0.08))

        st.markdown(f"""
**Aankopen per window · budget €{budget}**

| Moment | Actie | Kant | Bedrag |
|---|---|---|---|
| Window start | Volledige entry | Dominant | **€{budget}** (~{dom_shares} shares @ {dom_price}) |
| Prijs daalt > {avg_drop2:.0%} | Averaging down | Dominant (bijkopen) | **€{avg_down_ex}/tranche** (max €{avg_down_max2} totaal) |
| Andere kant > {flip_trig2} | Flip | Andere kant | **€{flip_tranche_ex}/tranche** elke {flip_int2}s (max €{flip_max_ex}) |
| Laatste {conf_secs2}s (≥{conf_trig2}) | Confirm buy | Winnende kant | **€{confirm_ex}** eenmalig |
| Verliezer ≤ {hedge_trig2} | Hedge | Verliezende kant | **€{hedge_eur_ex}** |

*Exit: hold to expiry (€1.00 per winnende share)*
""")

    with st.expander("📋 Strategie regels + Market gate"):
        _br = _CFG.get("bggdsb", {})
        _gate_min  = float(_br.get("entry_price_min", 0.10))
        _gate_max  = float(_br.get("entry_price_max", 0.90))
        _flip_tr   = float(_br.get("flip_trigger_price", 0.62))
        _flip_in   = int(_br.get("flip_interval_secs", 20))
        _conf_tr   = float(_br.get("confirm_trigger_price", 0.78))
        _conf_sc   = int(_br.get("confirm_secs_remaining", 90))
        _conf_pc   = float(_br.get("confirm_pct", 1.00))
        _hedge_tr  = float(_br.get("hedge_price_trigger", 0.11))
        _hedge_pc  = float(_br.get("hedge_size_pct", 0.10))

        rule_col, gate_col = st.columns(2)
        with rule_col:
            st.markdown(f"""
| Regel | Waarde |
|---|---|
| Market gate | Beide kanten {_gate_min} – {_gate_max} |
| Richting | OFI + funding rate (conviction) |
| Initiële entry | Volledig budget op dominante kant |
| Flip trigger | Andere kant > {_flip_tr} |
| Flip tranches | elke {_flip_in}s |
| Confirm buy | In laatste {_conf_sc}s (≥ {_conf_tr}) |
| Hedge timing | Verliezer ≤ {_hedge_tr} → {_hedge_pc:.0%} budget |
| Exit | Hold to expiry (€1.00) |
| Win-rate (is5 data) | **75.4%** initieel |
| ROI (is5 data) | **+26.6%** over 3 dagen |
""")
        with gate_col:
            saved_budget = int(get_state("bggdsb_window_budget") or 2)
            hedge_eur    = round(saved_budget * _hedge_pc, 2)
            confirm_eur2 = round(saved_budget * _conf_pc, 2)
            st.metric("Market gate", f"{_gate_min} – {_gate_max}")
            st.metric("Entry (volledig budget)", f"€{saved_budget:.2f}")
            st.metric(f"Hedge (bij ≤{_hedge_tr})", f"€{hedge_eur:.2f}")
            st.metric(f"Confirm (laatste {_conf_sc}s)", f"€{confirm_eur2:.2f}")
            st.metric("Actieve coins", ", ".join(_load_selected_coins()) or "—")
