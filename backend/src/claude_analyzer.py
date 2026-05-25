"""Claude AI integration — analyzes cycle trade data and suggests parameter optimizations."""
import json
import os
import re

import httpx

from .config_loader import CONFIG
from .db_sync import get_cycle_trades, get_cycle_stats, get_cycle_pnl_accuracy
from .logger import log

_CLAUDE_URL = "https://api.anthropic.com/v1/messages"

_SYSTEM_PROMPT = """You are the strategy optimizer for a Polymarket 5-minute binary options trading bot.

The bot trades YES/NO pairs on 5-minute crypto up/down markets (BTC, ETH, SOL, XRP, DOGE).
Strategy: buy both YES and NO at ~50¢ each. When one side moves to the trigger threshold,
sell the loser at market, then trail the winner with the peg-cross exit engine.

BREAK-EVEN MATH (critical):
  entry_cost  = (entry_yes + entry_no) × size   (typically ~1.04/share)
  loser_sell  = actual market bid at trigger time (NOT mid — always lower due to spread)
  break_even  = (entry_cost + fees − loser_proceeds) / size

Slippage reality: when loser mid is ~0.27 the market bid is typically ~0.17 (10¢ spread).
avg_loser_exit_price in the stats IS the actual received price including slippage.
Use avg_loser_exit_price — not (1 − trigger_threshold) — to estimate real break-even.

A trade is profitable only when avg_winner_exit_price > avg_break_even_price.
If avg_winner_exit_price < avg_break_even_price:
  • Trigger_threshold too low → winner doesn't run far enough before time expires.
  • Or held_for_resolution not firing — resolution ($1.00) would be profitable but winner
    is being sold early. If avg_winner_exit_price is close to 0.85, suggest lower
    hold_for_resolution_mid_threshold so more winners hold to $1.00 resolution.

Exit reasons:
- limit_filled: passive limit order filled (best outcome — patient fill at high price)
- peg_cross: cross_score exceeded threshold, converted to market sell
- force_exit_window_end: time ran out; sold at market
- held_for_resolution: held to window close, resolved at $1.00 (very profitable)

Parameter meanings:
- trigger_threshold: mid-price that fires the trade (0.65–0.85). Higher = stronger signal.
- cross_threshold: peg-cross score (0–1) to convert limit→market. Lower = more aggressive exits.
- initial_offset: limit starts at trigger_price + offset. Larger = more ambitious target.
- ratchet_buffer: how far above mid the resting limit sits. Larger = more patient.

Diagnosis shortcuts:
  high peg_cross rate       → trigger too low OR cross_threshold too low
  high limit_filled rate    → good — patient fills at high prices
  avg_winner < avg_break_even → raise trigger_threshold (primary lever) or lower cross_threshold
  avg_winner close to 0.85  → lower hold_for_resolution_mid_threshold so they go to $1.00

adaptive_stats shows real-time EMA of pnl/share and threshold adjustments already applied.

pnl_accuracy shows computed P&L (from trade DB) vs actual USDC delta since cycle start.
  pnl_accuracy_ratio near 1.0 = calculations match reality. Far from 1.0 = data is unreliable.
  Degrade confidence_score proportionally: if ratio < 0.7, cap confidence at 0.55.
  Note: held_for_resolution trades are excluded (their P&L is real but not yet in USDC balance).

global_params you can suggest: max_entry_cost, max_token_spread, hold_for_resolution_mid_threshold.
  hold_for_resolution_mid_threshold (0.60–0.85): at force_exit time, hold to $1.00 resolution
  when winner mid >= this threshold. Math: holding is always better in expectation above 0.65
  because resolution ($1.00) beats market-sell (bid = mid minus spread) in expected value.
  Suggest lower values (0.65–0.70) when trades are near break-even; higher (0.75–0.80) when
  you see frequent reversals in the trade sample.

Regime context (regime_stats in the prompt):
  TRENDING  — momentum continues; use patient cross_threshold (0.65–0.70), lower trigger ok
  CHOPPY    — winners reverse; raise trigger_threshold (+0.03–0.05), lower cross_threshold (0.45–0.55)
  RANGING   — asset oscillating in tight band; directional_bias is set (UP/DOWN);
              standard params but consider enabling the coin only when bias matches recent direction
  BREAKOUT  — fast explosive move; trigger fires quickly; lower initial_offset to capture early
  UNKNOWN   — insufficient data; keep conservative defaults

price_stats.price_position: 0.0 = asset at bottom of 30-min range, 1.0 = at top.
If RANGING and price_position >= 0.75 → recent direction is likely DOWN.
If RANGING and price_position <= 0.25 → recent direction is likely UP.
Suggest adjusted thresholds per coin based on its detected regime.

Respond with ONLY a JSON object. No markdown fences, no explanation outside the JSON."""


def _build_prompt(
    trades: list,
    stats: dict,
    current_params: dict,
    adaptive_stats: dict | None = None,
    pnl_accuracy: dict | None = None,
    regime_stats: dict | None = None,
    conviction_bucket_stats: dict | None = None,
    pattern_context: str | None = None,
) -> str:
    payload: dict = {
        "cycle_stats": stats,
        "current_params": current_params,
        "recent_trades_sample": trades,
        "task": (
            "Analyze and return optimized parameters. "
            "Confidence < 0.6 means stay in paper mode for another cycle. "
            "If pnl_accuracy_ratio is far from 1.0, reduce confidence score accordingly — "
            "the computed data may not reflect reality. "
            "cycle_stats now includes per-coin regime_distribution and conviction_distribution — "
            "use these to calibrate regime-specific params in the response."
        ),
    }
    if adaptive_stats:
        payload["adaptive_tuner_stats"] = adaptive_stats
    if pnl_accuracy:
        payload["pnl_accuracy"] = pnl_accuracy
    if regime_stats:
        payload["regime_stats"] = regime_stats
    if conviction_bucket_stats:
        payload["conviction_bucket_stats_alltime"] = conviction_bucket_stats
    if pattern_context:
        payload["current_market_pattern_vs_history"] = pattern_context
    return json.dumps(payload, indent=2)


async def analyze_cycle(cycle_id: int) -> dict:
    """
    Gather trade data for cycle_id, call Claude, parse and validate returned params.
    Returns the parsed parameter dict (confidence_score, coin_params, global_params, reasoning).
    """
    api_key = os.getenv("ANTHROPIC_API_KEY") or CONFIG.get("claude", {}).get("api_key", "")
    if not api_key or api_key.startswith("${"):
        raise ValueError("ANTHROPIC_API_KEY not set — cannot run analysis")

    model = CONFIG.get("claude", {}).get("model", "claude-sonnet-4-6")
    max_tokens = CONFIG.get("claude", {}).get("max_tokens", 4096)

    trades = get_cycle_trades(cycle_id, limit=50)
    stats = get_cycle_stats(cycle_id)

    try:
        from . import adaptive as _adaptive
        adaptive_stats = _adaptive.get_stats()
    except Exception:
        adaptive_stats = None

    current_params = {
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
            "hold_for_resolution_mid_threshold": CONFIG["exit"].get("hold_for_resolution_mid_threshold", 0.85),
        },
        "entry": {
            "max_combined_cost": CONFIG["entry"]["max_combined_cost"],
            "max_token_spread": CONFIG["entry"]["max_token_spread"],
        },
    }

    pnl_accuracy = get_cycle_pnl_accuracy(cycle_id)

    # Enrich with realized P&L from Polymarket US API (if credentials are set)
    try:
        from . import polymarket_us_api as _pm_us
        activities = await _pm_us.get_recent_activities(limit=200)
        if activities:
            api_pnl = _pm_us.compute_activities_pnl(activities)
            pnl_accuracy = {**(pnl_accuracy or {}), "api_realized_pnl": api_pnl}
    except Exception:
        pass

    # Regime stats (per-coin classification + price range context)
    try:
        from . import regime as _regime
        from .db_sync import get_cycle_trades as _gct
        regime_stats: dict | None = {}
        for coin in CONFIG.get("coins", {}):
            coin_trades = [t for t in (trades or []) if t.get("coin") == coin]
            label = _regime.detect_regime(coin, coin_trades)
            price_stats = _regime.get_asset_price_stats(coin)
            regime_stats[coin] = {
                "regime": label,
                "bias": _regime.get_directional_bias(coin),
                "price_stats": price_stats,
            }
    except Exception:
        regime_stats = None

    # Conviction bucket stats (all-time per coin — broader than cycle-only data)
    conviction_bucket_stats: dict | None = None
    try:
        from .db_sync import get_conviction_bucket_stats as _cbs
        conviction_bucket_stats = {
            coin: _cbs(coin=coin, days=30)
            for coin in CONFIG.get("coins", {})
        }
    except Exception:
        pass

    # Pattern matching context (current conditions vs historical outcomes)
    pattern_context: str | None = None
    try:
        from . import pattern_matcher as _pm
        pm_results = _pm.get_last_results()
        if pm_results:
            pattern_context = _pm.build_pattern_context_for_prompt(pm_results)
        else:
            # Compute on-the-fly if no cached results
            pm_results = _pm.run_pattern_backtest_sync()
            pattern_context = _pm.build_pattern_context_for_prompt(pm_results)
    except Exception:
        pass

    schema = """{
  "confidence_score": 0.0-1.0,
  "reasoning": "...",
  "prediction": {
    "expected_win_rate_pct": float,
    "expected_avg_pnl_per_trade": float,
    "key_assumption": "The single most important assumption this recommendation rests on — e.g. 'CHOPPY regime will persist for the next 20+ trades'.",
    "falsifiable_condition": "What outcome in the NEXT deploy phase would prove this assumption wrong — e.g. 'peg_cross_rate drops below 0.30 but win rate does not improve'.",
    "prediction_horizon_trades": int
  },
  "coin_params": {
    "BTC": {"trigger_threshold": float, "cross_threshold": float, "initial_offset": float, "ratchet_buffer": float, "enabled": bool},
    "ETH": { ... }, "SOL": { ... }, "XRP": { ... }, "DOGE": { ... }
  },
  "global_params": {
    "max_entry_cost": float,
    "max_token_spread": float,
    "hold_for_resolution_mid_threshold": float
  }
}"""

    user_prompt = (
        _build_prompt(
            trades, stats, current_params,
            adaptive_stats, pnl_accuracy, regime_stats,
            conviction_bucket_stats=conviction_bucket_stats,
            pattern_context=pattern_context,
        )
        + f"\n\nReturn this exact JSON structure:\n{schema}"
    )

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            _CLAUDE_URL,
            headers={
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "prompt-caching-2024-07-31",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "system": [
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [{"role": "user", "content": user_prompt}],
            },
        )
        resp.raise_for_status()
        content = resp.json()["content"][0]["text"]

    params = _parse_response(content)
    log.info("claude_analysis_done",
             cycle_id=cycle_id,
             confidence=params["confidence_score"],
             reasoning=params.get("reasoning", "")[:120])
    return params


def analyze_trades_sync(trades: list[dict], current_params: dict) -> dict:
    """Synchronous Claude analysis for direct use in Streamlit."""
    api_key = os.getenv("ANTHROPIC_API_KEY") or CONFIG.get("claude", {}).get("api_key", "")
    if not api_key or api_key.startswith("${"):
        raise ValueError("ANTHROPIC_API_KEY not set — cannot run analysis")

    model = CONFIG.get("claude", {}).get("model", "claude-sonnet-4-6")
    max_tokens = CONFIG.get("claude", {}).get("max_tokens", 4096)

    triggered = [t for t in trades if t.get("trigger_hit")]
    wins = [t for t in triggered if (t.get("net_pnl") or 0) > 0]
    loser_prices = [t["loser_exit_price"] for t in triggered if t.get("loser_exit_price") is not None]
    winner_prices = [t["winner_exit_price"] for t in triggered if t.get("winner_exit_price") is not None]
    be_prices = [t["break_even_price"] for t in triggered if t.get("break_even_price") is not None]
    stats = {
        "total_trades": len(trades),
        "triggered": len(triggered),
        "win_rate": round(len(wins) / max(len(triggered), 1) * 100, 1),
        "avg_pnl": round(sum(t.get("net_pnl") or 0 for t in triggered) / max(len(triggered), 1), 4),
        "total_pnl": round(sum(t.get("net_pnl") or 0 for t in trades), 4),
        "avg_loser_exit_price": round(sum(loser_prices) / len(loser_prices), 4) if loser_prices else None,
        "avg_winner_exit_price": round(sum(winner_prices) / len(winner_prices), 4) if winner_prices else None,
        "avg_break_even_price": round(sum(be_prices) / len(be_prices), 4) if be_prices else None,
    }

    try:
        from . import adaptive as _adaptive
        adaptive_stats = _adaptive.get_stats()
    except Exception:
        adaptive_stats = None

    schema = """{
  "confidence_score": 0.0-1.0,
  "reasoning": "...",
  "coin_params": {
    "BTC": {"trigger_threshold": float, "cross_threshold": float, "initial_offset": float, "ratchet_buffer": float, "enabled": bool},
    "ETH": { ... }, "SOL": { ... }, "XRP": { ... }, "DOGE": { ... }
  },
  "global_params": {"max_entry_cost": float, "max_token_spread": float}
}"""

    # Pattern context for manual analysis too
    pattern_context_sync: str | None = None
    try:
        from . import pattern_matcher as _pm
        pm_results = _pm.get_last_results() or _pm.run_pattern_backtest_sync()
        pattern_context_sync = _pm.build_pattern_context_for_prompt(pm_results)
    except Exception:
        pass

    sample = trades[-50:] if len(trades) > 50 else trades
    user_prompt = (
        _build_prompt(sample, stats, current_params, adaptive_stats,
                      pattern_context=pattern_context_sync)
        + f"\n\nReturn this exact JSON structure:\n{schema}"
    )

    with httpx.Client(timeout=120) as client:
        resp = client.post(
            _CLAUDE_URL,
            headers={
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "prompt-caching-2024-07-31",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "system": [
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [{"role": "user", "content": user_prompt}],
            },
        )
        resp.raise_for_status()
        content = resp.json()["content"][0]["text"]

    params = _parse_response(content)
    log.info("manual_claude_analysis_done",
             confidence=params["confidence_score"],
             reasoning=params.get("reasoning", "")[:120])
    return params


def _parse_response(text: str) -> dict:
    """Extract, validate and safety-clamp the JSON params from Claude's response."""
    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        raise ValueError(f"No JSON in Claude response: {text[:200]}")

    params = json.loads(match.group())

    if "confidence_score" not in params or "coin_params" not in params:
        raise ValueError(f"Missing required keys in Claude response: {list(params.keys())}")

    params["confidence_score"] = float(params["confidence_score"])
    if not 0.0 <= params["confidence_score"] <= 1.0:
        raise ValueError(f"confidence_score out of range: {params['confidence_score']}")

    BOUNDS = {
        "trigger_threshold": (0.65, 0.85),
        "cross_threshold": (0.30, 0.90),
        "initial_offset": (0.01, 0.15),
        "ratchet_buffer": (0.005, 0.05),
    }

    for coin, cp in params.get("coin_params", {}).items():
        for key, (lo, hi) in BOUNDS.items():
            if key in cp:
                val = float(cp[key])
                cp[key] = max(lo, min(hi, val))  # clamp silently instead of failing

    gp = params.get("global_params", {})
    if "max_entry_cost" in gp:
        gp["max_entry_cost"] = max(1.00, min(1.05, float(gp["max_entry_cost"])))
    if "max_token_spread" in gp:
        gp["max_token_spread"] = max(0.02, min(0.10, float(gp["max_token_spread"])))
    if "hold_for_resolution_mid_threshold" in gp:
        # Allow Claude to tune between 0.60 (aggressive hold) and 0.85 (conservative)
        gp["hold_for_resolution_mid_threshold"] = max(0.60, min(0.85, float(gp["hold_for_resolution_mid_threshold"])))

    return params


# ── Per-coin strategy revision (Herzie Strategie) ────────────────────────────

_COIN_SYSTEM_PROMPT = """Je bent een trading strategie optimizer voor een Polymarket 5-minuten binary opties bot.

De bot koopt BEIDE kanten (YES + NO) van een 5-minuten crypto up/down markt op ~50¢.
Wanneer één kant stijgt naar de trigger-drempel wordt de verliezende kant verkocht en
de winnende kant wordt getraild met het peg-cross exit systeem.

Break-even berekening:
  entry_cost = (entry_yes + entry_no) × size   (typisch ~1.04/share)
  loser bid op triggertijd ≈ mid − 10-15¢ (brede spread bij extreme prijzen)
  break_even = (entry_cost + fees − loser_proceeds) / winner_size

Parameteruitleg:
- trigger_threshold: drempel (0.65–0.85) waarop de trade vuurrt. Hoger = sterker signaal vereist.
- initial_offset: hoe ambitieus het eerste limiet-doel is (trigger + offset). Groter = hogere doelprijs.
- ratchet_buffer: hoe ver boven de mid het limiet-order blijft als de prijs stijgt. Groter = geduldiger.
- cross_threshold: peg-cross score (0–1) om van limiet naar markt te converteren. Lager = actiever uitstappen.
- early_loser_threshold: mid-prijs waarbij de verliezende kant vroeg verkocht wordt (b.v. 0.45).
- trigger_threshold_delta: correctie op de basis trigger_threshold voor dit regime.

Geef parameter-aanbevelingen per regime (TRENDING, CHOPPY, RANGING, BREAKOUT, NORMAL).
Antwoord ALLEEN in het gevraagde JSON formaat, geen markdown code blocks."""


def analyze_coin_strategy_sync(coin: str, trades: list[dict]) -> dict:  # noqa: C901
    """Sync Claude analysis for a single coin — called from Streamlit's Herzie Strategie button.

    Gathers ALL available data sources:
      - Trade aggregates + microstructure (spread, depth, velocity, timing)
      - Conviction bucket stats + threshold sweep
      - OFI bucket stats
      - Hourly P&L pattern
      - Early loser sell effectiveness
      - Break-even crossing analysis
      - Previous Claude / learning cycle findings
    """
    api_key = os.getenv("ANTHROPIC_API_KEY") or CONFIG.get("claude", {}).get("api_key", "")
    if not api_key or api_key.startswith("${"):
        raise ValueError("ANTHROPIC_API_KEY niet ingesteld — kan analyse niet uitvoeren")

    model = CONFIG.get("claude", {}).get("model", "claude-sonnet-4-6")
    max_tokens = int(CONFIG.get("claude", {}).get("max_tokens", 4096))

    # Detect the days window used (infer from trade timestamps for DB queries)
    from datetime import datetime, timezone, timedelta
    days: int | None = None
    if trades:
        oldest = min(
            (t.get("created_at") or "") for t in trades if t.get("created_at")
        )
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(oldest.replace("Z", "+00:00"))).days
            days = age + 1
        except Exception:
            days = 30

    # ── 1. Basic trade aggregates ─────────────────────────────────────────────
    from collections import defaultdict

    triggered = [t for t in trades if t.get("trigger_hit")]
    wins = [t for t in triggered if (t.get("net_pnl") or 0) > 0]
    total_pnl = sum(t.get("net_pnl") or 0 for t in triggered)

    loser_ps  = [t["loser_exit_price"]  for t in triggered if t.get("loser_exit_price")  is not None]
    winner_ps = [t["winner_exit_price"] for t in triggered if t.get("winner_exit_price") is not None]
    be_ps     = [t["break_even_price"]  for t in triggered if t.get("break_even_price")  is not None]

    # ── 2. Per-regime breakdown ───────────────────────────────────────────────
    by_regime: dict = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for t in triggered:
        r = t.get("regime_at_entry") or "NORMAL"
        by_regime[r]["n"] += 1
        by_regime[r]["pnl"] += t.get("net_pnl") or 0
        if (t.get("net_pnl") or 0) > 0:
            by_regime[r]["wins"] += 1

    # ── 3. Exit reason breakdown ──────────────────────────────────────────────
    by_exit: dict = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for t in triggered:
        e = t.get("winner_exit_reason") or "unknown"
        by_exit[e]["n"] += 1
        by_exit[e]["pnl"] += t.get("net_pnl") or 0

    # ── 4. Microstructure at trigger ──────────────────────────────────────────
    spreads   = [t["spread_at_trigger"]       for t in triggered if t.get("spread_at_trigger")       is not None]
    depths_y  = [t["yes_depth_at_trigger"]    for t in triggered if t.get("yes_depth_at_trigger")    is not None]
    depths_n  = [t["no_depth_at_trigger"]     for t in triggered if t.get("no_depth_at_trigger")     is not None]
    velocities = [t["mid_velocity_at_trigger"] for t in triggered if t.get("mid_velocity_at_trigger") is not None]
    timings   = [t["time_since_window_start"] for t in triggered if t.get("time_since_window_start") is not None]

    def _avg(lst): return sum(lst) / len(lst) if lst else None
    def _fmt(v, decimals=3): return f"{v:.{decimals}f}" if v is not None else "n.v.t."

    # ── 5. Signal quality (OFI, funding, liq) ─────────────────────────────────
    ofi_wins  = [t["ofi_at_trigger"] for t in triggered if t.get("ofi_at_trigger") is not None and (t.get("net_pnl") or 0) > 0]
    ofi_loss  = [t["ofi_at_trigger"] for t in triggered if t.get("ofi_at_trigger") is not None and (t.get("net_pnl") or 0) <= 0]
    conv_scores = [t["conviction_score_at_trigger"] for t in triggered if t.get("conviction_score_at_trigger") is not None]
    conv_wins = [t["conviction_score_at_trigger"] for t in triggered
                 if t.get("conviction_score_at_trigger") is not None and (t.get("net_pnl") or 0) > 0]
    conv_loss = [t["conviction_score_at_trigger"] for t in triggered
                 if t.get("conviction_score_at_trigger") is not None and (t.get("net_pnl") or 0) <= 0]

    # ── 6. Early loser sell effectiveness ────────────────────────────────────
    early_sells = [t for t in triggered if t.get("early_loser_side")]
    early_prices = [t["early_loser_price"] for t in early_sells if t.get("early_loser_price") is not None]
    # Estimate what loser would have fetched at trigger time (avg of non-early trades)
    trigger_loser_prices = [t["loser_exit_price"] for t in triggered
                            if not t.get("early_loser_side") and t.get("loser_exit_price") is not None]

    # ── 7. Break-even crossing analysis ──────────────────────────────────────
    above_be = [t for t in triggered
                if t.get("winner_exit_price") is not None and t.get("break_even_price") is not None
                and t["winner_exit_price"] > t["break_even_price"]]
    gaps_pos = [t["winner_exit_price"] - t["break_even_price"] for t in above_be]
    below_be = [t for t in triggered
                if t.get("winner_exit_price") is not None and t.get("break_even_price") is not None
                and t["winner_exit_price"] <= t["break_even_price"]]
    gaps_neg = [t["winner_exit_price"] - t["break_even_price"] for t in below_be]

    # ── 8. DB-backed bucket stats & sweep ────────────────────────────────────
    from .db_sync import (
        get_conviction_bucket_stats,
        get_conviction_threshold_sweep,
        get_ofi_bucket_stats,
        get_hourly_pnl,
        get_latest_completed_cycle,
        get_latest_manual_analysis,
    )

    def _safe(fn, **kw):
        try:
            return fn(**kw)
        except Exception:
            return []

    conv_buckets  = _safe(get_conviction_bucket_stats,  coin=coin, days=days)
    ofi_buckets   = _safe(get_ofi_bucket_stats,         coin=coin, days=days)
    sweep         = _safe(get_conviction_threshold_sweep, coin=coin, days=days)
    hourly        = _safe(get_hourly_pnl,               coin=coin, days=days)

    def _bucket_lines(rows, key="bucket"):
        return "\n".join(
            f"  {r.get(key,'?')}: {r.get('n',0)} trades, "
            f"{r.get('win_pct','?')}% win, gem €{r.get('avg_net_pnl',0):.4f}"
            for r in rows
        ) or "  (geen data)"

    sweep_lines = "\n".join(
        f"  >= {r.get('min_conviction',0):.1f}: {r.get('n',0)} trades, "
        f"{r.get('win_pct','?')}% win, totaal €{r.get('total_pnl',0):.2f}"
        for r in sweep
    ) or "  (geen data)"

    # Hourly: find best/worst 4h blocks
    hourly_summary = ""
    if hourly:
        by_block: dict = defaultdict(lambda: {"n": 0, "pnl": 0.0})
        for h in hourly:
            block = (int(h.get("hour_utc", 0)) // 4) * 4
            by_block[block]["n"] += h.get("n", 0)
            by_block[block]["pnl"] += h.get("total_pnl", 0)
        sorted_blocks = sorted(by_block.items(), key=lambda x: x[1]["pnl"])
        worst = sorted_blocks[:2]
        best  = sorted_blocks[-2:]
        hourly_summary = (
            "Beste tijdblokken (UTC):  " +
            ", ".join(f"{b:02d}:00–{b+4:02d}:00 (€{d['pnl']:+.2f}, {d['n']} trades)" for b, d in reversed(best)) +
            "\nSlechtste tijdblokken: " +
            ", ".join(f"{b:02d}:00–{b+4:02d}:00 (€{d['pnl']:+.2f}, {d['n']} trades)" for b, d in worst)
        )

    # ── 9. Previous analysis context ─────────────────────────────────────────
    prev_context = ""
    try:
        prev_manual = get_latest_manual_analysis()
        if prev_manual and prev_manual.get("coin_params", {}).get(coin):
            cp = prev_manual["coin_params"][coin]
            prev_context = (
                f"Vorige handmatige analyse ({prev_manual.get('created_at','?')[:10]}): "
                f"confidence={prev_manual.get('confidence_score','?')}, "
                f"aanbevolen params={json.dumps(cp)}\n"
                f"Redenering: {prev_manual.get('reasoning','')[:300]}"
            )
    except Exception:
        pass

    try:
        last_cycle = get_latest_completed_cycle()
        if last_cycle and last_cycle.get("claude_params"):
            cp_raw = last_cycle["claude_params"]
            cp = json.loads(cp_raw) if isinstance(cp_raw, str) else cp_raw
            coin_cp = cp.get("coin_params", {}).get(coin, {})
            if coin_cp:
                prev_context += (
                    f"\nLaatste learning cycle ({last_cycle.get('ended_at','?')[:10]}): "
                    f"confidence={cp.get('confidence_score','?')}, "
                    f"aanbevolen voor {coin}: {json.dumps(coin_cp)}"
                )
    except Exception:
        pass

    # ── 10. Current config ────────────────────────────────────────────────────
    coin_cfg    = CONFIG.get("coins", {}).get(coin, {})
    exit_cfg    = CONFIG.get("exit", {})
    trading_cfg = CONFIG.get("trading", {})

    # ── Assemble prompt ───────────────────────────────────────────────────────
    regime_lines = "\n".join(
        f"  {r}: {d['n']} trades, {d['wins']/max(d['n'],1)*100:.1f}% win, "
        f"gem €{d['pnl']/max(d['n'],1):.4f}, totaal €{d['pnl']:.2f}"
        for r, d in sorted(by_regime.items())
    )
    exit_lines = "\n".join(
        f"  {e}: {d['n']}× (gem €{d['pnl']/max(d['n'],1):.4f}, totaal €{d['pnl']:.2f})"
        for e, d in sorted(by_exit.items(), key=lambda x: -x[1]["n"])
    )

    stats_text = f"""
══ COIN: {coin}  |  ANALYSE PERIODE: {len(trades)} trades totaal, {len(triggered)} getriggerd ══

── KERN PERFORMANCE ─────────────────────────────────────────────────────────
Win rate:           {len(wins)/max(len(triggered),1)*100:.1f}%  ({len(wins)}/{len(triggered)})
Totaal P&L:         €{total_pnl:.2f}
Gem. P&L/trade:     €{total_pnl/max(len(triggered),1):.4f}
Gem. loser prijs:   {_fmt(_avg(loser_ps))} (werkelijk ontvangen incl. spread)
Gem. winner prijs:  {_fmt(_avg(winner_ps))}
Gem. break-even:    {_fmt(_avg(be_ps))}
Winner > BE:        {len(above_be)}/{len(triggered)} trades ({len(above_be)/max(len(triggered),1)*100:.1f}%)
  Gem. overschot:   +{_fmt(_avg(gaps_pos))} als winner > break-even
  Gem. tekort:      {_fmt(_avg(gaps_neg))} als winner < break-even

── RESULTATEN PER REGIME ────────────────────────────────────────────────────
{regime_lines}

── EXIT REDENEN ─────────────────────────────────────────────────────────────
{exit_lines}

── MARKTMICROSTRUCTUUR BIJ TRIGGER ──────────────────────────────────────────
Gem. spread bij trigger:     {_fmt(_avg(spreads))}  (bid-ask spread van winnende kant)
Gem. top-of-book diepte YES: {_fmt(_avg(depths_y), 2)} shares
Gem. top-of-book diepte NO:  {_fmt(_avg(depths_n), 2)} shares
Gem. prijs-velocity:         {_fmt(_avg(velocities))} per seconde
Gem. tijd na window-start:   {_fmt(_avg(timings), 0)} seconden  (hoe lang duurt trigger?)

── CONVICTION SIGNAAL KWALITEIT ─────────────────────────────────────────────
Gem. conviction score (wins):   {_fmt(_avg(conv_wins))}
Gem. conviction score (losses): {_fmt(_avg(conv_loss))}
Gem. OFI bij trigger (wins):    {_fmt(_avg(ofi_wins))}
Gem. OFI bij trigger (losses):  {_fmt(_avg(ofi_loss))}

Conviction buckets (win% + P&L per score-range):
{_bucket_lines(conv_buckets)}

OFI buckets (win% + P&L per OFI-range):
{_bucket_lines(ofi_buckets, key="bucket")}

Conviction threshold sweep (wat als we alleen handelen bij score >= X):
{sweep_lines}

── VROEG VERKOPEN EFFECTIVITEIT ─────────────────────────────────────────────
Vroeg verkochte losers:      {len(early_sells)} trades
Gem. vroeg-verkoop prijs:    {_fmt(_avg(early_prices))}
Gem. loser prijs bij trigger (overige trades): {_fmt(_avg(trigger_loser_prices))}
Vroeg-verkoop voordeel:      {_fmt((_avg(early_prices) or 0) - (_avg(trigger_loser_prices) or 0))} per share (positief = vroeg beter)

── TIJDSTIP ANALYSE (UTC) ───────────────────────────────────────────────────
{hourly_summary or "(geen data)"}

── HUIDIGE PARAMETERS VOOR {coin} ───────────────────────────────────────────
trigger_threshold:      {coin_cfg.get("trigger_threshold", trading_cfg.get("trigger_threshold", 0.73))}
initial_offset:         {coin_cfg.get("initial_offset", exit_cfg.get("initial_offset", 0.07))}
ratchet_buffer:         {coin_cfg.get("ratchet_buffer", exit_cfg.get("ratchet_buffer", 0.03))}
cross_threshold:        {coin_cfg.get("cross_threshold", exit_cfg.get("cross_threshold", 0.72))}
early_loser_threshold:  {exit_cfg.get("early_loser_threshold", 0.45)}
early_loser_rebuy:      {exit_cfg.get("early_loser_rebuy_threshold", 0.55)}
hold_for_resolution:    {exit_cfg.get("hold_for_resolution_mid_threshold", 0.55)}
force_exit_seconds:     {exit_cfg.get("force_exit_seconds", 30)}

Regime profielen (actief):
{json.dumps({k: v for k, v in CONFIG.get("regime_profiles", {}).items()}, indent=2)}
{f"── VORIGE ANALYSE CONTEXT ────────────────────────────────────────────────{chr(10)}{prev_context}" if prev_context else ""}"""

    schema = """{
  "coin": "TICKER",
  "performance_summary": "2-3 zinnen samenvatting in het Nederlands — wat gaat goed, wat niet",
  "key_issues": [
    "Meest urgente probleem in 1 zin",
    "Tweede probleem"
  ],
  "regime_specific": {
    "TRENDING": {
      "assessment": "Korte beoordeling (1-2 zinnen) in het Nederlands",
      "suggested_params": {
        "trigger_threshold_delta": 0.00,
        "initial_offset": 0.07,
        "cross_threshold": 0.72,
        "early_loser_threshold": 0.45,
        "early_loser_rebuy_threshold": null
      }
    },
    "CHOPPY":   {"assessment": "...", "suggested_params": {}},
    "RANGING":  {"assessment": "...", "suggested_params": {}},
    "BREAKOUT": {"assessment": "...", "suggested_params": {}},
    "NORMAL":   {"assessment": "...", "suggested_params": {}}
  },
  "global_coin_params": {
    "trigger_threshold": 0.75,
    "ratchet_buffer": 0.03,
    "hold_for_resolution_mid_threshold": 0.55
  },
  "conviction_gate_advice": {
    "recommended_min_score": 0.0,
    "reasoning": "Dutch explanation of whether a conviction gate helps"
  },
  "timing_advice": "Dutch text about time-of-day patterns, if actionable",
  "confidence": 0.70,
  "reasoning": "3-5 zinnen volledige redenering in het Nederlands"
}"""

    user_prompt = (
        f"{stats_text.strip()}\n\n"
        "Analyseer ALLE bovenstaande data zorgvuldig en geef een herziene handelsstrategie "
        f"voor {coin}. Gebruik de microstructuur, conviction buckets, threshold sweep en "
        "tijdstip-patronen als bewijs voor je aanbevelingen. "
        f"Return exact dit JSON schema:\n{schema}"
    )

    with httpx.Client(timeout=120) as client:
        resp = client.post(
            _CLAUDE_URL,
            headers={
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "prompt-caching-2024-07-31",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "system": [
                    {
                        "type": "text",
                        "text": _COIN_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [{"role": "user", "content": user_prompt}],
            },
        )
        resp.raise_for_status()
        content = resp.json()["content"][0]["text"]

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", content)
        result = json.loads(m.group()) if m else {
            "performance_summary": content[:500],
            "key_issues": [],
            "regime_specific": {},
            "global_coin_params": {},
            "confidence": 0.0,
            "reasoning": content,
        }

    log.info("coin_strategy_analysis_done", coin=coin,
             confidence=result.get("confidence", 0), n_trades=len(triggered))
    return result
