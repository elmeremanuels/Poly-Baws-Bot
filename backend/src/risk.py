"""Risk management: daily loss limits, kill switch, position limits."""
import asyncio
import os
from pathlib import Path
from datetime import datetime, timezone

from .config_loader import CONFIG
from .logger import log, get_daily_pnl, get_today_trade_count

KILL_FLAG_PATH = Path(__file__).parent.parent.parent / "KILL.flag"
_killed = False
_kill_reason = ""

# Global consecutive loss tracker (Component 3)
_global_consecutive_losses: int = 0

# Portfolio high-water mark (Component 5)
_portfolio_peak: float = 0.0


def is_killed() -> bool:
    return _killed or KILL_FLAG_PATH.exists()


def kill(reason: str = "manual") -> None:
    global _killed, _kill_reason
    _killed = True
    _kill_reason = reason
    KILL_FLAG_PATH.touch()
    log.critical("kill_switch_activated", reason=reason)


def reset_kill() -> None:
    global _killed, _kill_reason, _portfolio_peak
    _killed = False
    _kill_reason = ""
    _portfolio_peak = 0.0  # recalibrate drawdown-from-peak after manual resume
    if KILL_FLAG_PATH.exists():
        KILL_FLAG_PATH.unlink()
    log.info("kill_switch_reset")


def get_kill_reason() -> str:
    return _kill_reason


def _is_paper_like_mode(mode: str) -> bool:
    """True for any paper/simulation mode (no real money at stake)."""
    return mode.startswith("paper") or mode.endswith("_paper")


def _enforces_daily_loss_limit(mode: str) -> bool:
    """Whether the global daily-loss kill applies to this mode.

    Only real-money straddle modes (live_hybrid/live_auto) are subject to it.
    Exempt modes:
      - paper modes: simulated, no real money at stake.
      - live_learning: paper during learn/validate; its deploy phase has its own
        max_live_loss circuit breaker (orchestrator._tick_deploy).
      - signal_trader: has its own max_daily_loss_eur gate measured against only
        its own live trades (see signal_trader._daily_loss_exceeded). The global
        kill here is wrong because get_daily_pnl() sums ALL trades incl. the
        background paper straddle, so a few paper losses trip a €10 kill that
        immediately re-fires after every reset — making Resume appear dead.
      - bggdsb_live / bggdsb_paper: the €2/window budget means 5 losing windows
        (25 min) would trip a €10 kill. Portfolio protection (min_capital_eur,
        max_drawdown_from_start_pct) is the right risk control here.
    """
    return (not _is_paper_like_mode(mode)
            and mode != "live_learning"
            and mode != "signal_trader"
            and mode not in ("bggdsb_live", "bggdsb_paper"))


async def check_daily_loss_limit() -> bool:
    """Returns True if within daily loss limit. Only meaningful for live trades."""
    if is_killed():
        return False  # already killed — don't re-log
    daily_pnl = await get_daily_pnl()
    limit = CONFIG["risk"]["daily_loss_limit_eur"]
    if daily_pnl <= -limit:
        kill(f"daily_loss_limit_exceeded: {daily_pnl:.2f} EUR")
        return False
    return True


async def check_daily_trade_limit(coin: str | None = None) -> bool:
    count = await get_today_trade_count(coin)
    limit = CONFIG["risk"]["max_trades_per_day_total"]
    return count < limit


def check_global_position_limit(active_position_count: int) -> bool:
    return active_position_count < CONFIG["risk"]["global_max_positions"]


def check_coin_position_limit(coin: str, active_count: int) -> bool:
    coin_max = CONFIG["coins"][coin]["max_parallel_positions"]
    return active_count < coin_max


async def pre_trade_checks(coin: str, active_positions: dict[str, int],
                           mode: str = "live") -> tuple[bool, str]:
    """
    Run all pre-trade checks. Returns (ok, reason).
    active_positions: dict mapping coin -> count of active positions
    mode: current trading mode — loss limit is skipped for paper modes so paper
          trading can freely collect regime and pattern data without stopping.
    """
    if is_killed():
        return False, f"kill_switch_active: {_kill_reason}"

    if _enforces_daily_loss_limit(mode) and not await check_daily_loss_limit():
        return False, "daily_loss_limit_exceeded"

    if not await check_daily_trade_limit():
        return False, "daily_trade_limit_exceeded"

    total_active = sum(active_positions.values())
    if not check_global_position_limit(total_active):
        return False, f"global_position_limit_exceeded: {total_active}"

    coin_active = active_positions.get(coin, 0)
    if not check_coin_position_limit(coin, coin_active):
        return False, f"coin_position_limit_exceeded: {coin} {coin_active}"

    if not CONFIG["coins"].get(coin, {}).get("enabled", False):
        return False, f"coin_disabled: {coin}"

    from . import coin_guard as _cg
    if not _cg.can_enter(coin):
        state = _cg.get_coin_state(coin)
        return False, f"coin_guard_{state}: {coin}"

    return True, ""


# ── Component 3: Global Consecutive Loss Tracker ──────────────────────────────

def record_global_result(won: bool) -> None:
    """Call after every trade closes. Hard-kills the bot after N consecutive losses."""
    global _global_consecutive_losses
    if won:
        _global_consecutive_losses = 0
    else:
        _global_consecutive_losses += 1
        limit = CONFIG.get("risk", {}).get("max_consecutive_losses", 0)
        if limit > 0 and _global_consecutive_losses >= limit:
            kill(f"consecutive_loss_hard_stop:{_global_consecutive_losses}")


def get_consecutive_losses() -> int:
    return _global_consecutive_losses


# ── Component 5: Portfolio Protection ─────────────────────────────────────────

async def _get_or_update_portfolio_peak(current: float) -> float:
    global _portfolio_peak
    if current > _portfolio_peak:
        _portfolio_peak = current
        try:
            from .logger import save_dashboard_state
            await save_dashboard_state("portfolio_peak_usdc", str(round(current, 4)))
        except Exception:
            pass
    return _portfolio_peak


def _enforces_portfolio_protection(mode: str) -> bool:
    """Whether portfolio protection applies to this mode.

    Same exemptions as _enforces_daily_loss_limit: paper modes and live_learning
    simulate without risking real capital. bggdsb_live is real money so it IS
    subject to protection (use bggdsb_paper to trade without real-money risk gates).
    """
    return not _is_paper_like_mode(mode) and mode not in ("live_learning",)


async def check_portfolio_protection(current_usdc: float) -> None:
    """Three-layer portfolio protection. Call from portfolio_sync_loop."""
    if current_usdc is None:
        return
    if is_killed():
        return  # already killed — don't re-fire every 30s
    from .state import get_mode
    if not _enforces_portfolio_protection(get_mode()):
        return  # paper / live_learning: no real capital at risk
    cfg = CONFIG.get("risk", {})

    # Layer 1: absolute capital floor
    min_capital = cfg.get("min_capital_eur", 0.0)
    if min_capital > 0 and current_usdc < min_capital:
        kill(f"min_capital_floor:{current_usdc:.2f}<{min_capital:.2f}")
        return

    # Layer 2: drawdown from high-water mark
    max_dd_peak = cfg.get("max_drawdown_from_peak_pct", 0.0)
    if max_dd_peak > 0:
        peak = await _get_or_update_portfolio_peak(current_usdc)
        floor = peak * (1 - max_dd_peak / 100)
        if current_usdc < floor:
            kill(f"max_drawdown_from_peak:{current_usdc:.2f}<{floor:.2f}")
            return

    # Layer 3: drawdown from start capital
    max_dd_start = cfg.get("max_drawdown_from_start_pct", 0.0)
    if max_dd_start > 0:
        try:
            from .logger import load_dashboard_state
            start_raw = await load_dashboard_state("portfolio_start_usdc")
            start = float(start_raw) if start_raw else 0.0
        except Exception:
            start = 0.0
        if start > 0:
            floor_start = start * (1 - max_dd_start / 100)
            if current_usdc < floor_start:
                kill(f"max_drawdown_from_start:{current_usdc:.2f}<{floor_start:.2f}")


def reset_portfolio_peak() -> None:
    global _portfolio_peak
    _portfolio_peak = 0.0


async def risk_monitor_loop(get_active_count_fn, interval: float = 5.0) -> None:
    """Background loop that checks kill flag and loss limit periodically.

    Loss limit is only enforced in live modes — paper trading runs uncapped
    so it can collect as much signal data as possible.
    """
    global _killed, _kill_reason
    while True:
        try:
            if KILL_FLAG_PATH.exists() and not _killed:
                _killed = True
                _kill_reason = "file_flag"
                log.critical("kill_flag_file_detected")

            if not _killed:
                from .state import get_mode
                if _enforces_daily_loss_limit(get_mode()):
                    await check_daily_loss_limit()
        except Exception as e:
            log.error("risk_monitor_error", error=str(e))
        await asyncio.sleep(interval)
