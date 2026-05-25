"""Historical pattern matching — compare current market conditions to past trade outcomes.

A "fingerprint" captures 3 dimensions of market state:
  regime           : TRENDING / CHOPPY / RANGING / BREAKOUT / NORMAL / UNKNOWN
  conviction_bucket: none / low (0–0.3) / medium (0.3–0.6) / high (0.6–1.0)
  ofi_bucket       : bear (≤0.40) / neutral (0.40–0.60) / bull (≥0.60) / unknown

For each fingerprint combination the DB is queried for historical win_rate and avg_pnl.
Fallback hierarchy when no exact match: regime+conviction → regime only → all trades.

The 30-minute background task computes fingerprints for all enabled coins, matches them
against history, and saves the results to bot_state for the dashboard to display.
"""
import asyncio
import json
from datetime import datetime, timezone

from .config_loader import CONFIG
from .logger import log, save_dashboard_state
from .db_sync import get_pattern_stats, get_state as _db_get_state

_STATE_KEY = "pattern_match_results"
_TS_KEY    = "pattern_match_ts"

# In-memory cache of last results (also in DB for cross-process read)
_last_results: dict = {}
_last_ts: str = ""


# ── Fingerprint ────────────────────────────────────────────────────────────────

def get_current_fingerprint(coin: str) -> dict:
    """Return the current market state fingerprint for a coin."""
    try:
        from . import regime as _regime, signals as _sig
        current_regime = _regime.get_current_regime(coin) or "NORMAL"
        signals = _sig.get_all_signals(coin)
        ofi = signals.get("ofi")
        score = signals.get("conviction_score") or 0.0
    except Exception:
        current_regime, ofi, score = "NORMAL", None, 0.0

    if score == 0.0:
        conv_bucket = "none"
    elif score < 0.3:
        conv_bucket = "low"
    elif score < 0.6:
        conv_bucket = "medium"
    else:
        conv_bucket = "high"

    if ofi is None:
        ofi_bucket = "unknown"
    elif ofi <= 0.40:
        ofi_bucket = "bear"
    elif ofi >= 0.60:
        ofi_bucket = "bull"
    else:
        ofi_bucket = "neutral"

    return {"regime": current_regime, "conviction_bucket": conv_bucket, "ofi_bucket": ofi_bucket}


# ── Historical lookup ──────────────────────────────────────────────────────────

def match_fingerprint(coin: str, fingerprint: dict, days: int = 90) -> dict:
    """Find historical trades matching this fingerprint. Falls back to looser matches."""
    patterns = get_pattern_stats(coin=coin, days=days)

    def _search(regime=None, conv=None, ofi=None):
        return [
            p for p in patterns
            if (regime is None or p.get("regime") == regime)
            and (conv is None or p.get("conviction_bucket") == conv)
            and (ofi is None or p.get("ofi_bucket") == ofi)
        ]

    def _agg(rows, quality: str) -> dict:
        n = sum(r.get("n", 0) for r in rows)
        if n == 0:
            return {}
        win_pct = sum(r.get("win_pct", 0) * r.get("n", 0) for r in rows) / n
        avg_pnl = sum(r.get("avg_pnl", 0) * r.get("n", 0) for r in rows) / n
        return {
            "n": n,
            "win_pct": round(win_pct, 1),
            "avg_pnl": round(avg_pnl, 4),
            "match_quality": quality,
        }

    r = fingerprint.get("regime")
    c = fingerprint.get("conviction_bucket")
    o = fingerprint.get("ofi_bucket")

    # Exact
    hits = _search(r, c, o)
    if hits and sum(h.get("n", 0) for h in hits) >= 5:
        return _agg(hits, "exact")

    # Regime + conviction
    hits = _search(r, c)
    if hits and sum(h.get("n", 0) for h in hits) >= 5:
        return _agg(hits, "regime+conviction")

    # Regime only
    hits = _search(r)
    if hits and sum(h.get("n", 0) for h in hits) >= 5:
        return _agg(hits, "regime_only")

    # All trades (baseline)
    if patterns:
        return _agg(patterns, "baseline")

    return {}


# ── Batch run ──────────────────────────────────────────────────────────────────

def run_pattern_backtest_sync(coins: list[str] | None = None, days: int = 90) -> dict:
    """Compute fingerprints and historical matches for all enabled coins. Returns results dict."""
    if coins is None:
        coins = [c for c, cfg in CONFIG.get("coins", {}).items() if cfg.get("enabled", False)]

    results: dict = {}
    for coin in coins:
        fp = get_current_fingerprint(coin)
        match = match_fingerprint(coin, fp, days=days)
        results[coin] = {"fingerprint": fp, "match": match}
        log.info(
            "pattern_match",
            coin=coin,
            regime=fp["regime"],
            conviction=fp["conviction_bucket"],
            ofi=fp["ofi_bucket"],
            historical_win_pct=match.get("win_pct"),
            n=match.get("n"),
            quality=match.get("match_quality"),
        )

    return results


async def run_pattern_backtest(coins: list[str] | None = None, days: int = 90) -> dict:
    """Async wrapper — runs sync computation and persists to DB."""
    global _last_results, _last_ts
    results = run_pattern_backtest_sync(coins=coins, days=days)
    ts = datetime.now(timezone.utc).isoformat()
    _last_results = results
    _last_ts = ts
    await save_dashboard_state(_STATE_KEY, json.dumps(results))
    await save_dashboard_state(_TS_KEY, ts)
    return results


# ── Background loop ────────────────────────────────────────────────────────────

async def pattern_match_loop(interval_secs: int = 1800) -> None:
    """Background task: run pattern backtest every 30 minutes."""
    while True:
        try:
            await run_pattern_backtest()
        except Exception as exc:
            log.error("pattern_match_loop_error", error=str(exc))
        await asyncio.sleep(interval_secs)


# ── Read results (cross-process via DB) ───────────────────────────────────────

def get_last_results() -> dict:
    """Return last saved pattern match results (reads DB if in-memory is empty)."""
    if _last_results:
        return _last_results
    raw = _db_get_state(_STATE_KEY)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {}


def get_last_ts() -> str:
    return _last_ts or (_db_get_state(_TS_KEY) or "")


# ── Context string for Claude prompt ──────────────────────────────────────────

def build_pattern_context_for_prompt(results: dict) -> str:
    """Format pattern match results as a text block for the Claude analysis prompt."""
    if not results:
        return "(geen pattern match data beschikbaar)"

    lines = ["Huidige marktomstandigheden vs. historische patronen:"]
    for coin, data in results.items():
        fp = data.get("fingerprint", {})
        match = data.get("match", {})
        if not match:
            lines.append(f"  {coin}: {fp.get('regime','?')} / {fp.get('conviction_bucket','?')} / "
                         f"OFI {fp.get('ofi_bucket','?')} — geen historisch patroon gevonden")
        else:
            q = match.get("match_quality", "?")
            lines.append(
                f"  {coin}: {fp.get('regime','?')} / conv={fp.get('conviction_bucket','?')} / "
                f"OFI={fp.get('ofi_bucket','?')} → "
                f"{match.get('win_pct','?')}% win, gem €{match.get('avg_pnl',0):.4f} "
                f"(n={match.get('n','?')}, match={q})"
            )
    return "\n".join(lines)
