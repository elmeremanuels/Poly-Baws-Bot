"""Trade Router — scores every incoming market and routes it to the optimal bucket.

Buckets:
  signal       → signal_trader single-side entry (conviction 0.55–0.70)
  straddle_asym → asymmetric straddle (conviction ≥0.70, RANGING regime)
  straddle_sym  → symmetric straddle (conviction ≥0.70, other regimes)
  skip          → do not trade
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config_loader import CONFIG
from .logger import log


@dataclass
class TradeDecision:
    bucket: str          # "signal" | "straddle_asym" | "straddle_sym" | "skip"
    conviction_score: float
    regime: str
    conviction_dir: str | None
    yes_size: float      # recommended EUR stake on YES side
    no_size: float       # recommended EUR stake on NO side (0.0 for signal bucket)
    skip_reason: str = ""


def _skip(reason: str) -> TradeDecision:
    return TradeDecision(
        bucket="skip",
        conviction_score=0.0,
        regime="UNKNOWN",
        conviction_dir=None,
        yes_size=0.0,
        no_size=0.0,
        skip_reason=reason,
    )


def _correlated_risk_exceeded(direction: str | None) -> bool:
    """Block if ≥N open trades share the same direction bias."""
    if not direction:
        return False
    max_corr = CONFIG.get("router", {}).get("max_correlated_positions", 3)
    from .state import get_active_trades
    same = sum(
        1 for t in get_active_trades().values()
        if t.get("bias_direction_at_entry") == direction
    )
    return same >= max_corr


def route_trade(coin: str, market: dict | None = None) -> TradeDecision:
    """Score coin+market and return a routing decision.

    Called once per scan cycle per coin. The decision is re-validated at
    T-2min before window start (see bot.py final check).
    """
    cfg = CONFIG.get("router", {})
    if not cfg.get("enabled", True):
        return _skip("router_disabled")

    # ── Signal inputs ──────────────────────────────────────────────────────
    from .signals import get_conviction
    from .regime import get_current_regime

    conviction_dir, conviction_score = get_conviction(coin)
    regime = get_current_regime(coin) or "UNKNOWN"

    # ── Hard skips ─────────────────────────────────────────────────────────
    signal_min = cfg.get("signal_min_conviction", 0.55)
    if conviction_score < signal_min or conviction_dir is None:
        return _skip("low_conviction")

    from . import coin_guard as _cg
    if not _cg.can_enter(coin):
        state = _cg.get_coin_state(coin)
        return _skip(f"coin_guard_{state}")

    if _correlated_risk_exceeded(conviction_dir):
        return _skip("correlated_risk")

    # ── Routing: regime-gebaseerd (geen automatische signal_trader-voorrang) ──
    # Straddle = "koste wat het kost geen verlies" → veilig in onzekere regimes
    # Signal   = "maximale winst"                  → werkt bij duidelijke momentum
    #
    # RANGING / CHOPPY / UNKNOWN → straddle (richting onzeker, beide kanten kopen)
    # TRENDING / BREAKOUT        → signal (duidelijk momentum, één kant maximaal)
    # NORMAL                     → signal bij lage conviction, straddle bij hoge
    straddle_min = cfg.get("straddle_min_conviction", 0.70)
    trade_size = CONFIG.get("trading", {}).get("trade_size_eur", 1.0)

    _straddle_regimes = {"RANGING", "CHOPPY", "UNKNOWN"}
    _signal_regimes   = {"TRENDING", "BREAKOUT"}

    prefer_straddle = (
        regime in _straddle_regimes
        or conviction_score >= straddle_min  # hoge conviction → altijd straddle
    )

    if not prefer_straddle and conviction_score >= signal_min:
        # Signal trader: momentum-regime of NORMAL met matige conviction
        log.debug("route_decision", coin=coin, bucket="signal",
                  conviction=conviction_score, regime=regime)
        return TradeDecision(
            bucket="signal",
            conviction_score=conviction_score,
            regime=regime,
            conviction_dir=conviction_dir,
            yes_size=trade_size,
            no_size=0.0,
        )

    if conviction_score >= straddle_min:
        asym_regimes = cfg.get("straddle_asym_regime", ["RANGING"])
        if regime in asym_regimes:
            # Asymmetric: heavier stake on conviction side
            if conviction_dir == "UP":
                yes_size = round(trade_size * 0.65, 4)
                no_size = round(trade_size * 0.35, 4)
            else:
                yes_size = round(trade_size * 0.35, 4)
                no_size = round(trade_size * 0.65, 4)
            bucket = "straddle_asym"
        else:
            yes_size = no_size = round(trade_size * 0.50, 4)
            bucket = "straddle_sym"

        log.debug(
            "route_decision",
            coin=coin, bucket=bucket,
            conviction=conviction_score, regime=regime,
        )
        return TradeDecision(
            bucket=bucket,
            conviction_score=conviction_score,
            regime=regime,
            conviction_dir=conviction_dir,
            yes_size=yes_size,
            no_size=no_size,
        )

    return _skip("below_straddle_threshold")
