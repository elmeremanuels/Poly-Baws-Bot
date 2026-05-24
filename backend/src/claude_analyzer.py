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


def _build_prompt(trades: list, stats: dict, current_params: dict,
                  adaptive_stats: dict | None = None,
                  pnl_accuracy: dict | None = None,
                  regime_stats: dict | None = None) -> str:
    payload: dict = {
        "cycle_stats": stats,
        "current_params": current_params,
        "recent_trades_sample": trades,
        "task": (
            "Analyze and return optimized parameters. "
            "Confidence < 0.6 means stay in paper mode for another cycle. "
            "If pnl_accuracy_ratio is far from 1.0, reduce confidence score accordingly — "
            "the computed data may not reflect reality."
        ),
    }
    if adaptive_stats:
        payload["adaptive_tuner_stats"] = adaptive_stats
    if pnl_accuracy:
        payload["pnl_accuracy"] = pnl_accuracy
    if regime_stats:
        payload["regime_stats"] = regime_stats
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

    schema = """{
  "confidence_score": 0.0-1.0,
  "reasoning": "...",
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
        _build_prompt(trades, stats, current_params, adaptive_stats, pnl_accuracy, regime_stats)
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

    sample = trades[-50:] if len(trades) > 50 else trades
    user_prompt = _build_prompt(sample, stats, current_params, adaptive_stats) + f"\n\nReturn this exact JSON structure:\n{schema}"

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
