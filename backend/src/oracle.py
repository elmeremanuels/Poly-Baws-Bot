"""Oracle — vooruitkijkend beslissingsorgaan.

Berekent de Handels Temperatuur (0–100) en geeft een verdict vóór elke trade.
Hard gate blokkeert trades bij slechte macro-condities; track record bepaalt
hoeveel gewicht het Orakel meekreeg in het temperatuur-composiet.

Fase 1: temperatuur berekenen + logging, hard_gate=false (veilig deployen).
Fase 2: hard_gate=true activeren na ~50 verdicts met track record ≥ 0.55.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any

from .config_loader import CONFIG
from .logger import log


# ── Dataklassen ────────────────────────────────────────────────────────────────

@dataclass
class OracleContext:
    coin: str
    conviction_score: float
    regime: str
    fear_greed_value: int
    fear_greed_label: str
    news_sentiment: str
    news_sentiment_weighted: float
    top_headline: str | None
    polymarket_implied_dir: str
    track_record_score: float
    daily_outlook: str
    pattern_win_probability: float
    trading_temperature: int = 0


@dataclass
class OracleVerdict:
    approved: bool
    confidence: float
    reason: str
    discord_pending: bool
    trading_temperature: int
    macro_context: dict = field(default_factory=dict)


# ── Handels Temperatuur ────────────────────────────────────────────────────────

def compute_trading_temperature(ctx: OracleContext) -> int:
    """Composiet getal 0–100: 0=ijskoud, 100=heet. Hogere score = betere condities."""
    score = 0.0

    # Fear & Greed (25%) — 50 = neutraal, extremen = penalty
    fg = max(0, min(100, ctx.fear_greed_value))
    fg_score = 100 - abs(fg - 50) * 2        # 50→100pt, 0 of 100→0pt
    score += 0.25 * fg_score

    # Conviction (25%) — schaal 0.40–1.0 → 0–100
    conv = max(0.0, (ctx.conviction_score - 0.40) / 0.60) * 100
    score += 0.25 * conv

    # Nieuws sentiment (15%)
    news_map = {"positive": 80.0, "neutral": 50.0, "negative": 10.0, "unknown": 50.0}
    score += 0.15 * news_map.get(ctx.news_sentiment, 50.0)

    # Regime (15%)
    regime_map = {
        "RANGING": 90.0, "TRENDING": 70.0, "BREAKOUT": 75.0,
        "NORMAL": 60.0, "CHOPPY": 20.0, "UNKNOWN": 40.0,
    }
    score += 0.15 * regime_map.get(ctx.regime, 50.0)

    # Oracle track record (10%) — hoe beter Oracle presteert, hoe hoger temp
    score += 0.10 * (ctx.track_record_score * 100)

    # DB patronen (10%) — patroon win-kans voor huidige coin × 100
    score += 0.10 * (ctx.pattern_win_probability * 100)

    return max(0, min(100, round(score)))


# ── Track Record ───────────────────────────────────────────────────────────────

_verdict_history: deque[dict] = deque(maxlen=50)   # {"correct": bool, "ts": float}
_stored_verdicts: dict[str, OracleVerdict] = {}    # trade_id → verdict


def _compute_track_record() -> float:
    """Recency-weighted rolling accuracy over last 50 verdicts."""
    if not _verdict_history:
        return 0.5
    from .oracle_feeds import recency_weight
    now_ts = time.time()
    weighted_correct = 0.0
    weight_total = 0.0
    for v in _verdict_history:
        hours_ago = max(0.0, (now_ts - v["ts"]) / 3600)
        w = recency_weight(hours_ago)
        weighted_correct += w * (1.0 if v["correct"] else 0.0)
        weight_total += w
    if weight_total <= 0:
        return 0.5
    return round(weighted_correct / weight_total, 3)


def record_outcome(trade_id: str, won: bool) -> None:
    """Aanroepen na trade-afsluiting om track record bij te werken."""
    verdict = _stored_verdicts.get(trade_id)
    if not verdict:
        return
    correct = (verdict.approved == won)
    _verdict_history.append({"correct": correct, "ts": time.time()})
    log.info(
        "oracle_outcome_recorded",
        trade_id=trade_id,
        won=won,
        approved=verdict.approved,
        correct=correct,
        track_record=_compute_track_record(),
    )


# ── Daily outlook (gezet door 2× daagse analyse) ──────────────────────────────

_daily_outlook: str = "NEUTRAL"

def set_daily_outlook(outlook: str) -> None:
    global _daily_outlook
    _daily_outlook = outlook.upper()

def get_daily_outlook() -> str:
    return _daily_outlook


# ── Context bouwen ─────────────────────────────────────────────────────────────

async def _build_context(coin: str, conviction_score: float, regime: str) -> OracleContext:
    from .oracle_feeds import get_fear_greed, get_crypto_news_sentiment, get_polymarket_coin_sentiment
    from .oracle_patterns import get_current_pattern_win_probability

    # Parallel ophalen van externe data
    fg_data, news_data, pm_data = await asyncio.gather(
        get_fear_greed(),
        get_crypto_news_sentiment([coin]),
        get_polymarket_coin_sentiment(coin),
        return_exceptions=True,
    )

    fg = fg_data if isinstance(fg_data, dict) else {"value": 50, "label": "Neutral"}
    news_coin = (news_data or {}) if not isinstance(news_data, Exception) else {}
    news = news_coin.get(coin, {"sentiment": "unknown", "weighted_score": 0.0, "top_headline": None})
    pm = pm_data if isinstance(pm_data, dict) else {"avg_yes_price": 0.5, "n_markets": 0, "implied_direction": "NEUTRAL"}

    bucket = "unknown"

    pattern_prob = get_current_pattern_win_probability(
        coin=coin, regime=regime,
        conviction_score=conviction_score, bucket=bucket,
    )

    ctx = OracleContext(
        coin=coin,
        conviction_score=conviction_score,
        regime=regime,
        fear_greed_value=int(fg.get("value", 50)),
        fear_greed_label=str(fg.get("label", "Neutral")),
        news_sentiment=str(news.get("sentiment", "unknown")),
        news_sentiment_weighted=float(news.get("weighted_score", 0.0)),
        top_headline=news.get("top_headline"),
        polymarket_implied_dir=str(pm.get("implied_direction", "NEUTRAL")),
        track_record_score=_compute_track_record(),
        daily_outlook=_daily_outlook,
        pattern_win_probability=pattern_prob,
    )
    ctx.trading_temperature = compute_trading_temperature(ctx)
    return ctx


# ── Verdict ────────────────────────────────────────────────────────────────────

async def get_oracle_verdict(
    coin: str,
    conviction_score: float,
    regime: str,
    trade_id: str | None = None,
) -> OracleVerdict:
    """Geeft een OracleVerdict terug. Logt altijd; blokkeert alleen als hard_gate=true."""
    cfg = CONFIG.get("oracle", {})

    # Als Oracle uitstaat, altijd goedkeuren
    if not cfg.get("enabled", False):
        return OracleVerdict(
            approved=True, confidence=1.0, reason="oracle_disabled",
            discord_pending=False, trading_temperature=50,
        )

    ctx = await _build_context(coin, conviction_score, regime)
    temp = ctx.trading_temperature
    hard_gate = cfg.get("hard_gate", False)
    temp_hard = int(cfg.get("temperature_hard_block", 30))
    temp_soft = int(cfg.get("temperature_soft_block", 50))
    fg_val = ctx.fear_greed_value
    extreme_fear = int(cfg.get("extreme_fear_threshold", 15))
    extreme_greed = int(cfg.get("extreme_greed_threshold", 92))

    # ── Hard blocks ───────────────────────────────────────────────────────────
    reason = "approved"
    reject = False

    if fg_val <= extreme_fear:
        reason, reject = "extreme_fear_market", True
    elif fg_val >= extreme_greed:
        reason, reject = "extreme_greed_market", True
    elif ctx.news_sentiment == "negative" and fg_val < 40:
        reason, reject = "negative_news_bearish_market", True
    elif temp < temp_hard:
        reason, reject = "temp_too_cold", True

    # Soft block (Discord confirm — Fase 3)
    discord_pending = False
    if not reject and temp < temp_soft:
        if cfg.get("discord_confirm_enabled", False):
            discord_pending = True
            reason = "temp_soft_block_discord"

    approved = not reject and not discord_pending
    confidence = round(temp / 100.0, 2)

    verdict = OracleVerdict(
        approved=approved if hard_gate else True,
        confidence=confidence,
        reason=reason,
        discord_pending=discord_pending if hard_gate else False,
        trading_temperature=temp,
        macro_context={
            "fear_greed": fg_val,
            "fear_greed_label": ctx.fear_greed_label,
            "news_sentiment": ctx.news_sentiment,
            "news_weighted": ctx.news_sentiment_weighted,
            "polymarket_dir": ctx.polymarket_implied_dir,
            "regime": regime,
            "conviction": round(conviction_score, 3),
            "pattern_win_prob": ctx.pattern_win_probability,
            "track_record": ctx.track_record_score,
            "daily_outlook": ctx.daily_outlook,
            "top_headline": ctx.top_headline,
        },
    )

    log.info(
        "oracle_verdict",
        coin=coin,
        approved=verdict.approved,
        temperature=temp,
        reason=reason,
        hard_gate=hard_gate,
        fear_greed=fg_val,
        news=ctx.news_sentiment,
        track_record=ctx.track_record_score,
    )

    if trade_id:
        _stored_verdicts[trade_id] = verdict

    # Persist to DB (non-blocking — don't fail the trade if write fails)
    try:
        from .logger import write_oracle_verdict as _write_verdict
        import asyncio as _asyncio
        _asyncio.create_task(_write_verdict(
            trade_id=trade_id,
            coin=coin,
            approved=verdict.approved,
            confidence=verdict.confidence,
            reason=verdict.reason,
            trading_temperature=temp,
            macro_context=verdict.macro_context,
        ))
    except Exception:
        pass

    return verdict


# ── Snapshot voor dashboard ───────────────────────────────────────────────────

async def get_temperature_snapshot(coin: str, conviction_score: float, regime: str) -> dict:
    """Lichtgewicht snapshot voor het dashboard (geen verdict-logging)."""
    try:
        ctx = await _build_context(coin, conviction_score, regime)
        return {
            "temperature": ctx.trading_temperature,
            "fear_greed_value": ctx.fear_greed_value,
            "fear_greed_label": ctx.fear_greed_label,
            "news_sentiment": ctx.news_sentiment,
            "polymarket_dir": ctx.polymarket_implied_dir,
            "track_record": ctx.track_record_score,
            "pattern_win_prob": ctx.pattern_win_probability,
        }
    except Exception as exc:
        log.warning("oracle_snapshot_failed", error=str(exc))
        return {"temperature": 50}
