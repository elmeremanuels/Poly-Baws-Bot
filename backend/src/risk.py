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


def is_killed() -> bool:
    return _killed or KILL_FLAG_PATH.exists()


def kill(reason: str = "manual") -> None:
    global _killed, _kill_reason
    _killed = True
    _kill_reason = reason
    KILL_FLAG_PATH.touch()
    log.critical("kill_switch_activated", reason=reason)


def reset_kill() -> None:
    global _killed, _kill_reason
    _killed = False
    _kill_reason = ""
    if KILL_FLAG_PATH.exists():
        KILL_FLAG_PATH.unlink()
    log.info("kill_switch_reset")


def get_kill_reason() -> str:
    return _kill_reason


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
    """
    return (not mode.startswith("paper")
            and mode != "live_learning"
            and mode != "signal_trader")


async def check_daily_loss_limit() -> bool:
    """Returns True if within daily loss limit. Only meaningful for live trades."""
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
