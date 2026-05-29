"""Per-coin guard: loss streak, daily P&L cap, watch/disabled/paper_only states.

State machine per coin:
  active     → watch      (2 consecutive losses)
  watch      → active     (next trade wins — streak resets)
  watch      → paper_only (3rd consecutive loss — soft landing, paper trades only)
  watch      → disabled   (daily cap reached — hard stop)
  paper_only → active     (N consecutive paper wins — auto-recovery)
  paper_only → disabled   (if daily cap reached while in paper_only)
  disabled   → active     (manual re-enable via coin_guard_enable command)

Paper gate: after strategy revision, coin re-enters with a countdown of N paper
trades before the guard starts tracking live losses again.

State is persisted to the bot_state DB table so Streamlit can read it cross-process.
"""
import asyncio

from .config_loader import CONFIG
from .db_sync import get_state as _db_get_state
from .logger import log, save_dashboard_state

# In-memory cache (rebuilt lazily from DB on first access per coin)
_streak: dict[str, int] = {}
_state: dict[str, str] = {}
_disable_reason: dict[str, str] = {}
_paper_gate: dict[str, int] = {}
_paper_wins: dict[str, int] = {}   # consecutive paper wins in paper_only state
_loaded: set[str] = set()


def _daily_cap() -> float:
    return float(CONFIG.get("risk", {}).get("daily_coin_loss_limit_eur", 5.0))


def _paper_recovery_wins() -> int:
    return int(CONFIG.get("coin_guard", {}).get("paper_only_recovery_wins", 3))


def _load(coin: str) -> None:
    if coin in _loaded:
        return
    _loaded.add(coin)
    val = _db_get_state(f"cg_{coin}_state")
    _state[coin] = val if val in ("active", "watch", "disabled", "paper_only") else "active"
    val = _db_get_state(f"cg_{coin}_streak")
    _streak[coin] = int(val) if val and val.lstrip("-").isdigit() else 0
    val = _db_get_state(f"cg_{coin}_reason")
    _disable_reason[coin] = val or ""
    val = _db_get_state(f"cg_{coin}_paper_wins")
    _paper_wins[coin] = int(val) if val and val.isdigit() else 0


async def _persist(coin: str) -> None:
    await save_dashboard_state(f"cg_{coin}_state", _state.get(coin, "active"))
    await save_dashboard_state(f"cg_{coin}_streak", str(_streak.get(coin, 0)))
    await save_dashboard_state(f"cg_{coin}_reason", _disable_reason.get(coin, ""))
    await save_dashboard_state(f"cg_{coin}_paper_wins", str(_paper_wins.get(coin, 0)))


def get_coin_state(coin: str) -> str:
    _load(coin)
    return _state.get(coin, "active")


def get_coin_streak(coin: str) -> int:
    _load(coin)
    return _streak.get(coin, 0)


def get_disable_reason(coin: str) -> str:
    _load(coin)
    return _disable_reason.get(coin, "")


def get_paper_gate_remaining(coin: str) -> int:
    return _paper_gate.get(coin, 0)


def get_paper_wins(coin: str) -> int:
    _load(coin)
    return _paper_wins.get(coin, 0)


def can_enter(coin: str) -> bool:
    """True if new trades are allowed (active or paper_only — paper_only still enters but forced paper)."""
    return get_coin_state(coin) in ("active", "paper_only")


def is_paper_forced(coin: str) -> bool:
    """True when coin is in paper_only state — trades must be paper regardless of global mode."""
    return get_coin_state(coin) == "paper_only"


async def record_result(
    coin: str,
    net_pnl: float,
    daily_coin_pnl: float,
    paper: bool = False,
) -> str | None:
    """Update guard after a trade closes. Returns new state if it changed, else None."""
    _load(coin)

    # Paper gate: countdown, don't count toward streak
    if _paper_gate.get(coin, 0) > 0:
        _paper_gate[coin] -= 1
        remaining = _paper_gate[coin]
        log.info("coin_paper_gate_progress", coin=coin, remaining=remaining,
                 net_pnl=round(net_pnl, 4))
        if remaining == 0:
            log.info("coin_paper_gate_complete", coin=coin)
        return None

    current = _state.get(coin, "active")
    if current == "disabled":
        return None

    # ── paper_only recovery path ───────────────────────────────────────────────
    if current == "paper_only":
        if net_pnl >= 0 and paper:
            _paper_wins[coin] = _paper_wins.get(coin, 0) + 1
            needed = _paper_recovery_wins()
            wins = _paper_wins[coin]
            log.info("coin_paper_only_win", coin=coin, paper_wins=wins, needed=needed)
            if wins >= needed:
                _state[coin] = "active"
                _streak[coin] = 0
                _paper_wins[coin] = 0
                _disable_reason[coin] = ""
                await _persist(coin)
                log.info("coin_paper_only_recovered", coin=coin, paper_wins=wins)
                return "active"
        elif net_pnl < 0:
            # Loss resets paper win streak (but stays in paper_only)
            _paper_wins[coin] = 0
            # Hard stop if daily cap blown even in paper_only
            cap = _daily_cap()
            if not paper and daily_coin_pnl <= -cap:
                await _do_disable(coin, f"dag-cap bereikt in paper_only ({daily_coin_pnl:.2f} EUR)")
                return "disabled"
        await _persist(coin)
        return None

    if net_pnl >= 0:
        old_streak = _streak.get(coin, 0)
        _streak[coin] = 0
        if current == "watch":
            _state[coin] = "active"
            _disable_reason[coin] = ""
            await _persist(coin)
            log.info("coin_watch_cleared", coin=coin, prev_streak=old_streak)
            return "active"
        if old_streak > 0:
            await _persist(coin)
        return None

    # Loss path
    _streak[coin] = _streak.get(coin, 0) + 1
    streak = _streak[coin]
    cap = _daily_cap()

    # Paper trades (learning, signal_lab_bg) never trigger coin disabling —
    # the coin guard only protects against LIVE monetary losses.
    if paper:
        await _persist(coin)
        return None

    if daily_coin_pnl <= -cap:
        await _do_disable(coin, f"dag-cap bereikt ({daily_coin_pnl:.2f} EUR ≤ -{cap:.0f})")
        return "disabled"

    if current == "watch":
        # Soft landing: paper_only instead of hard disabled
        _state[coin] = "paper_only"
        _streak[coin] = 0
        _paper_wins[coin] = 0
        _disable_reason[coin] = f"3e verlies op rij → paper_only herstel (streak={streak})"
        await _persist(coin)
        log.warning("coin_enter_paper_only", coin=coin, streak=streak,
                    recovery_wins_needed=_paper_recovery_wins())
        return "paper_only"

    if streak >= 2:
        _state[coin] = "watch"
        await _persist(coin)
        log.warning("coin_enter_watch_mode", coin=coin, streak=streak)
        return "watch"

    await _persist(coin)
    return None


async def _do_disable(coin: str, reason: str) -> None:
    _state[coin] = "disabled"
    _disable_reason[coin] = reason
    await _persist(coin)
    log.warning("coin_disabled_by_guard", coin=coin, reason=reason)


async def enable_coin(coin: str) -> None:
    """Re-enable a disabled/watch/paper_only coin. Must be called from the bot async process."""
    _load(coin)
    _state[coin] = "active"
    _streak[coin] = 0
    _paper_wins[coin] = 0
    _disable_reason[coin] = ""
    _paper_gate.pop(coin, None)
    await _persist(coin)
    log.info("coin_guard_enabled", coin=coin)


def start_paper_gate(coin: str, n: int = 5) -> None:
    """Start an N-trade paper countdown before live guard resumes tracking."""
    _paper_gate[coin] = n
    log.info("coin_paper_gate_started", coin=coin, n=n)


def get_all_states() -> dict[str, dict]:
    coins = list(CONFIG.get("coins", {}).keys())
    return {
        coin: {
            "state": get_coin_state(coin),
            "streak": get_coin_streak(coin),
            "reason": get_disable_reason(coin),
            "paper_gate": _paper_gate.get(coin, 0),
            "paper_wins": _paper_wins.get(coin, 0),
            "paper_recovery_needed": _paper_recovery_wins(),
        }
        for coin in coins
    }
