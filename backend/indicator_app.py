"""BTC Directional Indicator — met S/R niveaus en context-berichten.

Signalen (Kraken REST, geen WebSocket):
  OFI spot   35%  /Trades 60s
  OBI        28%  /Depth 25 niveaus
  Momentum   20%  VWAP-drift 45s
  Perp OFI   12%  Futures /history
  CVD slope   5%  acceleratie

S/R niveaus:
  Orderboek walls  — grote bid/ask clusters (live, < 1.5% van prijs)
  OHLC pivots      — dagelijkse H/L (gisteren + vandaag)
  Volume nodes     — meest verhandelde $100-buckets (uit trade buffer)

Usage:
    streamlit run backend/indicator_app.py --server.port 8502 --server.headless true
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
import streamlit as st

# Allow importing from src/ when run as a top-level Streamlit script
sys.path.insert(0, str(Path(__file__).parent))

from src.indicator_engine import (  # noqa: E402
    Cache,
    get_cache,
    compute_direction,
    compile_levels,
    wall_message,
    compute_book_walls,
    compute_vpoc_levels,
)

_CLAUDE_URL = "https://api.anthropic.com/v1/messages"


# ── Cache (delegated to shared engine, shared across both apps in same process) ─

@st.cache_resource
def _cache() -> Cache:
    return get_cache("BTC")


# ── Sound ──────────────────────────────────────────────────────────────────────

def _play(direction: str) -> None:
    if direction == "up":
        s = ("var ctx=new(window.AudioContext||window.webkitAudioContext)();"
             "var o1=ctx.createOscillator(),o2=ctx.createOscillator(),g=ctx.createGain();"
             "o1.connect(g);o2.connect(g);g.connect(ctx.destination);"
             "o1.frequency.value=660;o2.frequency.value=880;o1.type='sine';o2.type='sine';"
             "g.gain.setValueAtTime(0.25,ctx.currentTime);"
             "g.gain.exponentialRampToValueAtTime(0.001,ctx.currentTime+0.35);"
             "o1.start(ctx.currentTime);o1.stop(ctx.currentTime+0.15);"
             "o2.start(ctx.currentTime+0.12);o2.stop(ctx.currentTime+0.35);")
    else:
        s = ("var ctx=new(window.AudioContext||window.webkitAudioContext)();"
             "var o1=ctx.createOscillator(),o2=ctx.createOscillator(),g=ctx.createGain();"
             "o1.connect(g);o2.connect(g);g.connect(ctx.destination);"
             "o1.frequency.value=440;o2.frequency.value=330;o1.type='sine';o2.type='sine';"
             "g.gain.setValueAtTime(0.25,ctx.currentTime);"
             "g.gain.exponentialRampToValueAtTime(0.001,ctx.currentTime+0.35);"
             "o1.start(ctx.currentTime);o1.stop(ctx.currentTime+0.15);"
             "o2.start(ctx.currentTime+0.12);o2.stop(ctx.currentTime+0.35);")
    st.components.v1.html(f"<script>{s}</script>", height=0)


# ── UI helpers ─────────────────────────────────────────────────────────────────

def _levels_html(price: float, supports: list, resistances: list) -> str:
    """Compacte prijs-niveaus tabel: weerstand boven, support onder."""
    rows: list[str] = []

    def src_tag(s: dict) -> str:
        if s["src"] == "book": return "wall"
        if s["src"] == "vpoc": return "vol"
        return s["label"]

    for lvl in reversed(resistances[:2]):
        pct = (lvl["price"] - price) / price * 100
        rows.append(
            f'<tr>'
            f'<td style="color:#ef4444;font-family:monospace;padding:1px 2px;">{lvl["price"]:,.0f}</td>'
            f'<td style="color:#666;font-size:9px;padding:1px 3px;">{src_tag(lvl)}</td>'
            f'<td style="color:#ef4444;text-align:right;padding:1px 2px;">+{pct:.1f}%</td>'
            f'</tr>'
        )

    rows.append(
        f'<tr style="background:#222;border-top:1px solid #444;border-bottom:1px solid #444;">'
        f'<td style="color:#e0e0e0;font-family:monospace;font-weight:700;padding:2px 2px;">{price:,.0f}</td>'
        f'<td style="color:#888;font-size:9px;padding:2px 3px;">nu</td>'
        f'<td></td>'
        f'</tr>'
    )

    for lvl in supports[:2]:
        pct = (price - lvl["price"]) / price * 100
        rows.append(
            f'<tr>'
            f'<td style="color:#22c55e;font-family:monospace;padding:1px 2px;">{lvl["price"]:,.0f}</td>'
            f'<td style="color:#666;font-size:9px;padding:1px 3px;">{src_tag(lvl)}</td>'
            f'<td style="color:#22c55e;text-align:right;padding:1px 2px;">-{pct:.1f}%</td>'
            f'</tr>'
        )

    if not rows:
        return '<div style="font-size:9px;color:#555;text-align:center;">Niveaus laden…</div>'

    return (
        f'<div style="font-size:10px;margin-top:6px;border-top:1px solid #1e1e1e;padding-top:5px;">'
        f'<table style="width:100%;border-collapse:collapse;">{"".join(rows)}</table>'
        f'</div>'
    )


# ── App ────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="BTC", page_icon="📈", layout="centered")
st.markdown("""
<style>
  #MainMenu, header, footer, [data-testid="stDecoration"],
  [data-testid="stToolbar"] { display: none !important; }
  .block-container {
    padding: 0.4rem 0.5rem 0 !important;
    max-width: 270px !important;
    margin: 0 auto;
  }
  div[data-testid="stCheckbox"] label { font-size: 11px !important; }
</style>
""", unsafe_allow_html=True)

cache = _cache()

if "sound_on"  not in st.session_state: st.session_state["sound_on"]  = True
if "prev_dir"  not in st.session_state: st.session_state["prev_dir"]  = "undecided"

sound_on = st.checkbox("🔊 Geluid", value=st.session_state["sound_on"], key="sound_cb")
st.session_state["sound_on"] = sound_on


@st.fragment(run_every=2)
def _panel() -> None:
    with cache.lock:
        n      = len(cache.spot)
        s_ok   = cache.spot_ok
        p_ok   = cache.perp_ok
        b_ok   = cache.book_ok
        last_t = cache.last_poll_ts

    age = int(time.time() - last_t) if last_t else None

    if not s_ok or n < 20:
        st.markdown(f"""
<div style="background:#f9731618;border:2px solid #f97316;border-radius:10px;
            padding:12px 6px;text-align:center;font-family:system-ui;">
  <div style="font-size:32px;">⏳</div>
  <div style="font-size:14px;font-weight:700;color:#f97316;">{"Ophalen…" if not last_t else "Verbinden…"}</div>
  <div style="font-size:10px;color:#888;margin-top:4px;">{n} trades</div>
</div>""", unsafe_allow_html=True)
        return

    direction, score, d = compute_direction(cache)
    price, supports, resistances = compile_levels(cache)
    wall_msg, wall_color = wall_message(price, supports, resistances, direction)

    with cache.lock:
        regime = cache.regime

    prev = st.session_state.get("prev_dir", "undecided")
    if st.session_state.get("sound_on") and direction != "undecided" and direction != prev:
        _play(direction)
    st.session_state["prev_dir"] = direction

    if direction == "up":
        base_color, label, arrow, emoji = "#22c55e", "STIJGING",  "▲", "🟢"
    elif direction == "down":
        base_color, label, arrow, emoji = "#ef4444", "DALING",    "▼", "🔴"
    else:
        base_color, label, arrow, emoji = "#f97316", "UNDECIDED", "◆", "🟠"

    color = wall_color if wall_color and wall_msg else base_color

    rc    = {"RANGING": "#22c55e", "TRENDING": "#3b82f6",
             "CHOPPY":  "#ef4444"}.get(regime, "#888")
    age_s = f"{age}s" if age is not None else "—"

    ofi_s = d["spot_ofi"]
    obi_s = d["obi"]
    cvd_s = d["cvd"]
    mom_s = d["mom"]

    def _bar(val: float | None, bull: bool) -> str:
        if val is None: return '<span style="color:#555">—</span>'
        frac = max(0.0, min(1.0, val)); filled = round(frac * 5)
        clr  = "#22c55e" if bull else "#ef4444"
        return f'<span style="color:{clr};font-family:monospace">{"█"*filled}{"░"*(5-filled)}</span>'

    mom_html = ("—" if mom_s is None else
                f'<span style="color:{"#22c55e" if mom_s>0 else "#ef4444"}">'
                f'{"▲" if mom_s>0 else "▼"} {abs(mom_s):.2f}</span>')

    wall_row = (
        f'<div style="font-size:10px;color:{color};font-weight:600;'
        f'background:{color}22;border-radius:4px;padding:2px 4px;margin:4px 0 2px;">'
        f'{wall_msg}</div>'
        if wall_msg else ""
    )

    levels_html = _levels_html(price, supports, resistances) if price else ""

    st.markdown(f"""
<div style="background:{color}18;border:2px solid {color};border-radius:10px;
            padding:10px 6px 8px;text-align:center;font-family:system-ui;">
  <div style="font-size:38px;line-height:1.1;">{emoji}</div>
  <div style="font-size:21px;font-weight:800;color:{color};margin-top:2px;">{arrow} {label}</div>
  <div style="margin-top:4px;">
    <span style="font-size:10px;color:{rc};font-weight:700;
                 background:{rc}22;padding:1px 6px;border-radius:4px;">{regime}</span>
    <span style="font-size:10px;color:#888;margin-left:4px;">score {score:.2f}</span>
  </div>
  {wall_row}
  <table style="width:100%;margin-top:4px;font-size:10px;color:#aaa;border-collapse:collapse;">
    <tr>
      <td style="text-align:left;color:#666;">OFI</td>
      <td style="text-align:right;">{_bar(ofi_s, True)}</td>
      <td style="text-align:right;color:#555;">{f"{ofi_s:.3f}" if ofi_s is not None else "—"}</td>
    </tr>
    <tr>
      <td style="text-align:left;color:#666;">OBI</td>
      <td style="text-align:right;">{_bar(obi_s, True)}</td>
      <td style="text-align:right;color:#555;">{f"{obi_s:.3f}" if obi_s is not None else "—"}</td>
    </tr>
    <tr>
      <td style="text-align:left;color:#666;">MOM</td>
      <td colspan="2" style="text-align:right;">{mom_html}</td>
    </tr>
    <tr>
      <td style="text-align:left;color:#666;">CVD</td>
      <td colspan="2" style="text-align:right;color:#555;">
        {f"{cvd_s:+.3f}" if cvd_s is not None else "—"}
      </td>
    </tr>
  </table>
  {levels_html}
  <div style="font-size:9px;color:#444;margin-top:4px;border-top:1px solid #2a2a2a;padding-top:4px;">
    {"🟢" if s_ok else "🔴"}OFI {"🟢" if b_ok else "🟡"}Book {"🟢" if p_ok else "🟡"}Perp
    <span style="float:right;">⟳{age_s}</span>
  </div>
</div>""", unsafe_allow_html=True)


_panel()


# ── Claude advies (1× per 5 minuten) ─────────────────────────────────────────

def _call_claude(direction: str, score: float, d: dict,
                 price: float, supports: list, resistances: list, regime: str) -> dict:
    """Synchrone call naar Claude Haiku 4.5. Retourneert dict met verdict/confidence/reason."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"verdict": "—", "confidence": 0.0, "reason": "ANTHROPIC_API_KEY niet ingesteld."}

    def _fmt(v) -> str:
        return f"{v:.3f}" if v is not None else "n/b"

    sup_str = f"${supports[0]['price']:,.0f}"  if supports    else "—"
    res_str = f"${resistances[0]['price']:,.0f}" if resistances else "—"

    user_msg = (
        f"BTC perpetual Hyperliquid signalen:\n"
        f"Richting={direction} Score={score:.2f} Regime={regime}\n"
        f"OFI={_fmt(d.get('spot_ofi'))} OBI={_fmt(d.get('obi'))} "
        f"MOM={_fmt(d.get('mom'))} CVD={_fmt(d.get('cvd'))}\n"
        f"Prijs=${price:,.0f} Support={sup_str} Weerstand={res_str}\n\n"
        'Antwoord uitsluitend als JSON: {"verdict":"LONG"|"SHORT"|"FLAT",'
        '"confidence":0.0-1.0,"reason":"max 2 zinnen"}'
    )
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                _CLAUDE_URL,
                headers={
                    "content-type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-haiku-4-5",
                    "max_tokens": 150,
                    "system": (
                        "Je bent een BTC perpetuals handelaar op Hyperliquid. "
                        "OFI>0=koopdruck, CVD negatief=netto verkoop, score 0–1 is "
                        "gewogen signaalsterkte. Geef advies als JSON, altijd geldig JSON."
                    ),
                    "messages": [{"role": "user", "content": user_msg}],
                },
            )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"]
        m = re.search(r'\{[^{}]+\}', text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as exc:
        return {"verdict": "—", "confidence": 0.0, "reason": f"Fout: {str(exc)[:100]}"}
    return {"verdict": "—", "confidence": 0.0, "reason": "Ongeldig antwoord van Claude."}


@st.fragment(run_every=300)
def _claude_panel() -> None:
    with cache.lock:
        s_ok  = cache.spot_ok
        n     = len(cache.spot)
    if not s_ok or n < 20:
        return

    direction, score, d = compute_direction(cache)
    price, supports, resistances = compile_levels(cache)
    with cache.lock:
        regime = cache.regime

    result     = _call_claude(direction, score, d, price, supports, resistances, regime)
    verdict    = result.get("verdict", "—")
    confidence = float(result.get("confidence", 0.0))
    reason     = result.get("reason", "")

    vclr  = {"LONG": "#22c55e", "SHORT": "#ef4444", "FLAT": "#f97316"}.get(verdict, "#555")
    arrow = {"LONG": "▲",       "SHORT": "▼",       "FLAT": "◆"}.get(verdict, "")
    ts    = time.strftime("%H:%M")

    st.markdown(f"""
<div style="background:{vclr}18;border:2px solid {vclr};border-radius:10px;
            padding:10px 8px 8px;text-align:center;font-family:system-ui;margin-top:8px;">
  <div style="font-size:10px;color:#888;margin-bottom:4px;letter-spacing:.04em;">
    🤖 CLAUDE ADVIES &nbsp;·&nbsp; {int(confidence * 100)}% overtuiging
  </div>
  <div style="font-size:26px;font-weight:800;color:{vclr};line-height:1.1;">
    {arrow} {verdict}
  </div>
  <div style="font-size:10px;color:#bbb;margin-top:6px;text-align:left;
              line-height:1.45;padding:0 2px;">
    {reason}
  </div>
  <div style="font-size:9px;color:#444;margin-top:5px;border-top:1px solid #2a2a2a;
              padding-top:3px;">
    ⟳ elke 5 min &nbsp;·&nbsp; {ts}
  </div>
</div>""", unsafe_allow_html=True)


_claude_panel()


# ── Snap reversal events ───────────────────────────────────────────────────────

def _snap_section() -> None:
    from src.snap_reversal import get_detector
    det    = get_detector("BTC")
    events = det.recent_events()
    signal = det.hedge_signal()

    if not events and signal is None:
        return

    now = time.time()
    st.markdown(
        "<div style='margin-top:8px;font-size:11px;color:#f59e0b;font-weight:700;"
        "letter-spacing:.05em;'>⚡ SNAP REVERSALS (8h)</div>",
        unsafe_allow_html=True,
    )

    if signal:
        arrow = "▲" if signal["hedge_direction"] == "UP" else "▼"
        st.markdown(
            f"<div style='background:#f59e0b22;border:1px solid #f59e0b;border-radius:6px;"
            f"padding:6px 8px;font-size:11px;color:#f59e0b;margin:2px 0 4px;'>"
            f"<b>⚡ HEDGE SIGNAAL</b> {arrow} {signal['hedge_direction']} — "
            f"snap {signal['snap_direction']} {signal['snap_magnitude']:.2f}% — "
            f"conf {signal['confidence']:.0%} — {signal['prior_count']}× bevestigd"
            f"{'  @' + str(int(signal['near_round'])) if signal.get('near_round') else ''}"
            f"</div>",
            unsafe_allow_html=True,
        )

    rows = []
    for e in reversed(events[-8:]):
        age  = now - e.ts
        mins = int(age // 60)
        secs = int(age % 60)
        age_s = f"{mins}m{secs:02d}s"
        arrow = "▲" if e.direction == "UP" else "▼"
        clr   = "#22c55e" if e.direction == "UP" else "#ef4444"
        rnd   = f" @{int(e.near_round)}" if e.near_round else ""
        rows.append(
            f"<tr><td style='color:{clr};font-weight:700;'>{arrow} {e.direction}</td>"
            f"<td style='color:#ddd;'>{e.magnitude_pct:.2f}%</td>"
            f"<td style='color:#888;'>{age_s}{rnd}</td></tr>"
        )

    if rows:
        st.markdown(
            "<table style='width:100%;font-size:10px;font-family:monospace;"
            "border-collapse:collapse;'>"
            + "".join(rows) + "</table>",
            unsafe_allow_html=True,
        )


_snap_section()
