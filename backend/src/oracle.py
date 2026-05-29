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


# ── Dagelijkse Claude-analyse (2× per dag) ────────────────────────────────────

async def run_daily_analysis() -> dict | None:
    """Run 2x daily deep analysis via Claude API. Returns analysis dict or None on failure.

    Collects: last 24h trade stats per coin/regime, oracle track record, Fear&Greed,
    top headlines, current regime per coin, pattern stats.
    Sends prompt to Claude (claude-haiku-4-5-20251001 for speed/cost).
    Saves to oracle_analyses DB table.
    Sends to Discord via oracle_discord.send_daily_analysis().
    """
    import os
    import httpx
    from .db_sync import get_analytics_trades
    from .oracle_feeds import get_fear_greed, get_crypto_news_sentiment
    from .oracle_patterns import get_pattern_stats

    cfg = CONFIG.get("oracle", {})
    if not cfg.get("enabled", False):
        log.info("run_daily_analysis_skipped", reason="oracle_disabled")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.warning("run_daily_analysis_skipped", reason="no_anthropic_api_key")
        return None

    # ── Gather data ───────────────────────────────────────────────────────────
    trades_24h: list[dict] = []
    try:
        trades_24h = get_analytics_trades(days=1)
    except Exception as exc:
        log.warning("run_daily_analysis_trades_error", error=str(exc))

    fg_data: dict = {}
    news_data: dict = {}
    try:
        fg_data, news_data = await asyncio.gather(
            get_fear_greed(),
            get_crypto_news_sentiment(None),
            return_exceptions=False,
        )
    except Exception as exc:
        log.warning("run_daily_analysis_feeds_error", error=str(exc))

    pattern_data: list[dict] = []
    try:
        pattern_data = get_pattern_stats(days=7)
    except Exception as exc:
        log.warning("run_daily_analysis_patterns_error", error=str(exc))

    # ── Summarise trades per coin ─────────────────────────────────────────────
    coin_stats: dict[str, dict] = {}
    for t in trades_24h:
        c = t.get("coin", "UNKNOWN")
        if c not in coin_stats:
            coin_stats[c] = {"wins": 0, "losses": 0, "pnl": 0.0, "regime": t.get("regime", "UNKNOWN")}
        pnl = float(t.get("pnl", 0) or 0)
        coin_stats[c]["pnl"] = round(coin_stats[c]["pnl"] + pnl, 4)
        if pnl > 0:
            coin_stats[c]["wins"] += 1
        else:
            coin_stats[c]["losses"] += 1

    fg_value = int((fg_data or {}).get("value", 50))
    fg_label = str((fg_data or {}).get("label", "Neutral"))
    track_record = _compute_track_record()

    # Top headlines (max 3, across all coins in news_data)
    headlines: list[str] = []
    if isinstance(news_data, dict):
        for coin_news in news_data.values():
            hl = (coin_news or {}).get("top_headline")
            if hl and hl not in headlines:
                headlines.append(hl)
            if len(headlines) >= 3:
                break

    # Pattern summary (top 3 by win_rate desc)
    sorted_patterns = sorted(
        [p for p in (pattern_data or []) if isinstance(p, dict)],
        key=lambda p: float(p.get("win_rate", 0)),
        reverse=True,
    )[:3]
    pattern_summary = [
        f"{p.get('coin','?')} {p.get('pattern','?')}: {p.get('win_rate',0):.0%} wr ({p.get('count',0)} trades)"
        for p in sorted_patterns
    ]

    # ── Build compact prompt (~800 tokens) ───────────────────────────────────
    coin_lines = "\n".join(
        f"  {c}: {s['wins']}W/{s['losses']}L  pnl={s['pnl']:+.2f}  regime={s['regime']}"
        for c, s in coin_stats.items()
    ) or "  (no trades in last 24h)"

    headline_lines = "\n".join(f"  - {h}" for h in headlines) or "  (none)"
    pattern_lines = "\n".join(f"  {p}" for p in pattern_summary) or "  (no pattern data)"

    prompt = (
        "You are a crypto trading analyst. Analyse the following snapshot and respond "
        "with a single valid JSON object — no markdown, no extra text.\n\n"
        f"Fear & Greed: {fg_value} ({fg_label})\n"
        f"Oracle track record (last 50): {track_record:.1%}\n\n"
        "24h trade results per coin:\n"
        f"{coin_lines}\n\n"
        "Top headlines:\n"
        f"{headline_lines}\n\n"
        "Pattern stats (top 3 by win rate, last 7d):\n"
        f"{pattern_lines}\n\n"
        "Respond with JSON matching exactly this schema:\n"
        "{\n"
        '  "market_outlook": "<BULLISH|NEUTRAL|BEARISH>",\n'
        '  "risk_level": "<LOW|MEDIUM|HIGH>",\n'
        '  "coin_sentiments": {"<COIN>": "<BULLISH|NEUTRAL|BEARISH>"},\n'
        '  "pattern_insights": "<1-2 sentences>",\n'
        '  "suggested_adjustments": "<1-2 sentences>",\n'
        '  "reasoning": "<2-3 sentences>",\n'
        '  "discord_summary": "<max 280 chars for Discord>"\n'
        "}"
    )

    # ── Call Claude API ────────────────────────────────────────────────────────
    result: dict | None = None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 512,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            raw_text = resp.json()["content"][0]["text"]
            result = json.loads(raw_text)
    except Exception as exc:
        log.error("run_daily_analysis_claude_error", error=str(exc))
        return None

    # ── Post-process ──────────────────────────────────────────────────────────
    try:
        set_daily_outlook(result.get("market_outlook", "NEUTRAL"))
    except Exception as exc:
        log.warning("run_daily_analysis_set_outlook_error", error=str(exc))

    # Persist to DB (non-blocking, graceful if function not yet available)
    try:
        from .logger import write_oracle_analysis as _write_analysis
        asyncio.create_task(_write_analysis(
            market_outlook=result.get("market_outlook", "NEUTRAL"),
            risk_level=result.get("risk_level", "MEDIUM"),
            coin_sentiments=result.get("coin_sentiments", {}),
            pattern_insights=result.get("pattern_insights", ""),
            suggested_adjustments=result.get("suggested_adjustments", ""),
            reasoning=result.get("reasoning", ""),
            discord_summary=result.get("discord_summary", ""),
            fear_greed_value=fg_value,
            track_record=track_record,
        ))
    except (ImportError, AttributeError):
        pass  # write_oracle_analysis not yet implemented — skip silently
    except Exception as exc:
        log.warning("run_daily_analysis_db_write_error", error=str(exc))

    # Send to Discord
    try:
        from .oracle_discord import send_daily_analysis as _send_daily
        asyncio.create_task(_send_daily(result))
    except (ImportError, AttributeError):
        pass  # oracle_discord not yet wired up
    except Exception as exc:
        log.warning("run_daily_analysis_discord_error", error=str(exc))

    log.info(
        "run_daily_analysis_complete",
        market_outlook=result.get("market_outlook"),
        risk_level=result.get("risk_level"),
        fear_greed=fg_value,
        track_record=track_record,
    )
    return result


# ── Active Oracle-bewaking tijdens een live trade ─────────────────────────────

async def watch_trade_oracle(
    trade_id: str,
    coin: str,
    conviction_score: float,
    regime: str,
    window_end: "datetime",
) -> None:
    """Active Oracle monitoring during a live/paper trade.

    Runs every 30s until window_end. On each tick:
    - Recomputes temperature
    - If temp drops > 20 points from entry temp: sends Discord warning
    - If temp drops below temperature_hard_block: sends urgent Discord alert
      + optionally kills the trade (if hard_gate enabled)
    """
    from datetime import datetime, timezone

    cfg = CONFIG.get("oracle", {})
    if not cfg.get("enabled", False):
        log.debug("watch_trade_oracle_skipped", trade_id=trade_id, reason="oracle_disabled")
        return

    temp_hard = int(cfg.get("temperature_hard_block", 30))
    hard_gate = cfg.get("hard_gate", False)
    poll_interval = 30  # seconds

    # ── Baseline temperature at trade entry ───────────────────────────────────
    try:
        entry_snapshot = await get_temperature_snapshot(coin, conviction_score, regime)
        entry_temp: int = int(entry_snapshot.get("temperature", 50))
    except Exception as exc:
        log.warning("watch_trade_oracle_entry_error", trade_id=trade_id, error=str(exc))
        entry_temp = 50

    log.info(
        "watch_trade_oracle_start",
        trade_id=trade_id,
        coin=coin,
        entry_temp=entry_temp,
        window_end=str(window_end),
    )

    drop_alerted = False      # avoid repeat temperature_drop alerts
    hard_block_alerted = False  # avoid repeat hard_block alerts

    # ── Poll loop ─────────────────────────────────────────────────────────────
    while True:
        now = datetime.now(timezone.utc)
        # Normalise window_end to UTC-aware if naive
        _window_end = window_end
        if hasattr(window_end, "tzinfo") and window_end.tzinfo is None:
            _window_end = window_end.replace(tzinfo=timezone.utc)

        if now >= _window_end:
            log.debug("watch_trade_oracle_window_expired", trade_id=trade_id)
            break

        await asyncio.sleep(poll_interval)

        # Re-check after sleep in case window expired during wait
        now = datetime.now(timezone.utc)
        if now >= _window_end:
            break

        try:
            snapshot = await get_temperature_snapshot(coin, conviction_score, regime)
            current_temp: int = int(snapshot.get("temperature", entry_temp))
        except Exception as exc:
            log.warning("watch_trade_oracle_tick_error", trade_id=trade_id, error=str(exc))
            continue

        temp_drop = entry_temp - current_temp
        log.debug(
            "watch_trade_oracle_tick",
            trade_id=trade_id,
            coin=coin,
            entry_temp=entry_temp,
            current_temp=current_temp,
            temp_drop=temp_drop,
        )

        # ── Temperature drop >= 20 warning ───────────────────────────────────
        if temp_drop >= 20 and not drop_alerted:
            drop_alerted = True
            log.warning(
                "watch_trade_oracle_temp_drop",
                trade_id=trade_id,
                coin=coin,
                entry_temp=entry_temp,
                current_temp=current_temp,
                temp_drop=temp_drop,
            )
            try:
                from .oracle_discord import send_mid_trade_alert as _alert
                asyncio.create_task(_alert(
                    trade_id=trade_id,
                    coin=coin,
                    alert_type="temperature_drop",
                    entry_temp=entry_temp,
                    current_temp=current_temp,
                    temp_drop=temp_drop,
                    snapshot=snapshot,
                ))
            except (ImportError, AttributeError):
                pass
            except Exception as exc:
                log.warning("watch_trade_oracle_discord_drop_error", trade_id=trade_id, error=str(exc))

        # ── Hard block threshold breach ───────────────────────────────────────
        if current_temp < temp_hard and not hard_block_alerted:
            hard_block_alerted = True
            log.warning(
                "watch_trade_oracle_hard_block",
                trade_id=trade_id,
                coin=coin,
                current_temp=current_temp,
                temp_hard=temp_hard,
                hard_gate=hard_gate,
            )
            try:
                from .oracle_discord import send_mid_trade_alert as _alert
                asyncio.create_task(_alert(
                    trade_id=trade_id,
                    coin=coin,
                    alert_type="hard_block_breach",
                    entry_temp=entry_temp,
                    current_temp=current_temp,
                    temp_drop=temp_drop,
                    snapshot=snapshot,
                    hard_gate=hard_gate,
                    # Discord message will include a manual close prompt — bot does NOT
                    # auto-abort the trade; operator decision required via Discord.
                ))
            except (ImportError, AttributeError):
                pass
            except Exception as exc:
                log.warning("watch_trade_oracle_discord_hard_block_error", trade_id=trade_id, error=str(exc))

    log.info(
        "watch_trade_oracle_done",
        trade_id=trade_id,
        coin=coin,
        entry_temp=entry_temp,
    )


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
