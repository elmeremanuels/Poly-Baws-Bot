"""Claude AI integration — analyzes cycle trade data and suggests parameter optimizations."""
import json
import os
import re

import httpx

from .config_loader import CONFIG
from .db_sync import get_cycle_trades, get_cycle_stats
from .logger import log

_CLAUDE_URL = "https://api.anthropic.com/v1/messages"

_SYSTEM_PROMPT = """You are the strategy optimizer for a Polymarket 5-minute binary options trading bot.

The bot trades YES/NO pairs on 5-minute crypto up/down markets (BTC, ETH, SOL, XRP, DOGE).
Strategy: buy both YES and NO at ~50¢ each. When one side moves to the trigger threshold,
sell the loser at market, then trail the winner with the peg-cross exit engine.

Exit reasons:
- limit_filled: passive limit order filled (best outcome — patient fill at high price)
- peg_cross: cross_score exceeded threshold, converted to market sell
- force_exit_window_end: time ran out

Parameter meanings:
- trigger_threshold: mid-price level that fires the trade (0.65-0.85)
- cross_threshold: peg-cross score (0-1) to convert limit→market. Lower = more aggressive exits.
- initial_offset: limit starts at trigger_price + offset. Larger = more ambitious target.
- ratchet_buffer: how far above mid the ratcheted limit sits. Larger = more patient.

High stop rate / peg_cross rate = trigger too low or parameters too aggressive.
High limit_filled rate = good (patient fills, better prices).
Low win_rate with high avg_peak_bid = exits too early (cross_threshold too low).

Respond with ONLY a JSON object. No markdown fences, no explanation outside the JSON."""


def _build_prompt(trades: list, stats: dict, current_params: dict) -> str:
    return json.dumps({
        "cycle_stats": stats,
        "current_params": current_params,
        "recent_trades_sample": trades,
        "task": (
            "Analyze and return optimized parameters. "
            "Confidence < 0.6 means stay in paper mode for another cycle."
        ),
    }, indent=2)


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
        },
        "entry": {
            "max_combined_cost": CONFIG["entry"]["max_combined_cost"],
            "max_token_spread": CONFIG["entry"]["max_token_spread"],
        },
    }

    schema = """{
  "confidence_score": 0.0-1.0,
  "reasoning": "...",
  "coin_params": {
    "BTC": {"trigger_threshold": float, "cross_threshold": float, "initial_offset": float, "ratchet_buffer": float, "enabled": bool},
    "ETH": { ... }, "SOL": { ... }, "XRP": { ... }, "DOGE": { ... }
  },
  "global_params": {"max_entry_cost": float, "max_token_spread": float}
}"""

    user_prompt = _build_prompt(trades, stats, current_params) + f"\n\nReturn this exact JSON structure:\n{schema}"

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
    stats = {
        "total_trades": len(trades),
        "triggered": len(triggered),
        "win_rate": round(len(wins) / max(len(triggered), 1) * 100, 1),
        "avg_pnl": round(sum(t.get("net_pnl") or 0 for t in triggered) / max(len(triggered), 1), 4),
        "total_pnl": round(sum(t.get("net_pnl") or 0 for t in trades), 4),
    }

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
    user_prompt = _build_prompt(sample, stats, current_params) + f"\n\nReturn this exact JSON structure:\n{schema}"

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

    return params
