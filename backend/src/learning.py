"""Learning cycle orchestrator — manages the learn→analyze→deploy→validate cycle."""
import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

from .config_loader import CONFIG
from .logger import log, _db
from .db_sync import get_phase_stats, get_cycle_trades, get_cycle_stats

# ── Module-level state (read by bot.py to stamp trades) ───────────────────────
_current_cycle_id: Optional[int] = None
_current_phase: str = "manual"
_analyzing: bool = False

# ── Apply-learnings state (used by live_auto + apply toggle) ─────────────────
_applied_learned_params: dict = {}  # originals saved before applying


def get_current_cycle_id() -> Optional[int]:
    return _current_cycle_id


def get_current_phase() -> str:
    return _current_phase


def is_analyzing() -> bool:
    return _analyzing


# ── DB helpers (async, bot-side) ──────────────────────────────────────────────

async def _db_create_cycle(cycle_number: int, params_used: str) -> int:
    async with _db() as db:
        cursor = await db.execute(
            "INSERT INTO learning_cycles (cycle_number, phase, phase_started_at, started_at, params_used) "
            "VALUES (?, 'learn', datetime('now'), datetime('now'), ?)",
            (cycle_number, params_used),
        )
        await db.commit()
        return cursor.lastrowid


async def _db_update_cycle(cycle_id: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [cycle_id]
    async with _db() as db:
        await db.execute(f"UPDATE learning_cycles SET {sets} WHERE id=?", vals)
        await db.commit()


async def _db_get_cycle(cycle_id: int) -> dict | None:
    async with _db() as db:
        db.row_factory = __import__("aiosqlite").Row
        async with db.execute(
            "SELECT * FROM learning_cycles WHERE id=?", (cycle_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def _db_get_phase_triggered_count(cycle_id: int, phase: str) -> int:
    async with _db() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM trades WHERE cycle_id=? AND phase=? AND trigger_hit=1",
            (cycle_id, phase),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0


async def _db_get_phase_pnl(cycle_id: int, phase: str) -> float:
    async with _db() as db:
        async with db.execute(
            "SELECT COALESCE(SUM(net_pnl),0) FROM trades WHERE cycle_id=? AND phase=? AND status IN ('closed','resolved')",
            (cycle_id, phase),
        ) as cur:
            row = await cur.fetchone()
        return float(row[0]) if row else 0.0


async def _db_recent_peg_cross_rate(cycle_id: int, n: int = 10) -> float:
    async with _db() as db:
        async with db.execute(
            """SELECT COUNT(*),
               SUM(CASE WHEN winner_exit_reason='peg_cross' THEN 1 ELSE 0 END)
               FROM trades WHERE cycle_id=? AND phase='deploy' AND trigger_hit=1
               ORDER BY created_at DESC LIMIT ?""",
            (cycle_id, n),
        ) as cur:
            row = await cur.fetchone()
        total, peg = row if row else (0, 0)
        return (peg / total) if total and total >= n else 0.0


# ── Orchestrator ──────────────────────────────────────────────────────────────

_SAFETY_BOUNDS = {
    "trigger_threshold": (0.65, 0.85),
}
ABSOLUTE_MIN_PAPER_TRADES = 100
MAX_CYCLE_LOSS = 10.00


class LearningOrchestrator:
    def __init__(self):
        self._cycle_id: Optional[int] = None
        self._cycle_number: int = 0
        self._phase: str = "manual"
        self._phase_started_at: Optional[datetime] = None
        self._active_params: Optional[dict] = None
        self._original_coin_cfg: dict = {}
        self._started: bool = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        # Resume existing open cycle if any
        async with _db() as db:
            db.row_factory = __import__("aiosqlite").Row
            async with db.execute(
                "SELECT * FROM learning_cycles WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
        if row:
            existing = dict(row)
            self._cycle_id = existing["id"]
            self._cycle_number = existing["cycle_number"]
            self._set_phase(existing["phase"], reset_time=False)
            ps = existing.get("phase_started_at")
            if ps:
                try:
                    self._phase_started_at = datetime.fromisoformat(ps).astimezone(timezone.utc)
                except Exception:
                    self._phase_started_at = datetime.now(timezone.utc)
            log.info("learning_resumed", cycle=self._cycle_number, phase=self._phase)
        else:
            await self._start_new_cycle()

    async def _start_new_cycle(self) -> None:
        self._cycle_number += 1
        params_json = json.dumps(self._current_config_snapshot())
        cycle_id = await _db_create_cycle(self._cycle_number, params_json)
        self._cycle_id = cycle_id
        self._set_phase("learn")
        log.info("learning_cycle_started", cycle=self._cycle_number)

    def _set_phase(self, phase: str, reset_time: bool = True) -> None:
        global _current_cycle_id, _current_phase
        self._phase = phase
        _current_phase = phase
        _current_cycle_id = self._cycle_id
        if reset_time:
            self._phase_started_at = datetime.now(timezone.utc)

    def _phase_hours(self) -> float:
        if not self._phase_started_at:
            return 0.0
        return (datetime.now(timezone.utc) - self._phase_started_at).total_seconds() / 3600

    async def tick(self) -> None:
        if not self._cycle_id:
            return
        phase = self._phase
        if phase == "learn":
            await self._tick_learn()
        elif phase == "analyze":
            await self._tick_analyze()
        elif phase == "deploy":
            await self._tick_deploy()
        elif phase == "validate":
            await self._tick_validate()

    async def _tick_learn(self) -> None:
        lcfg = CONFIG["learning"]
        per_coin_needed = lcfg["min_trades_per_coin"]
        max_hours = lcfg["max_learn_hours"]
        # Count enabled coins
        enabled_coins = [c for c, v in CONFIG["coins"].items() if v.get("enabled", True)]
        total_needed = per_coin_needed * len(enabled_coins)
        count = 0
        if self._cycle_id:
            async with _db() as db:
                async with db.execute(
                    "SELECT COUNT(*) FROM trades WHERE cycle_id=? AND phase='learn'",
                    (self._cycle_id,),
                ) as cur:
                    row = await cur.fetchone()
                count = row[0] if row else 0
        if count >= total_needed or self._phase_hours() >= max_hours:
            await self._transition_to("analyze")

    async def _tick_analyze(self) -> None:
        global _analyzing
        if _analyzing:
            return  # already running
        _analyzing = True
        try:
            from .claude_analyzer import analyze_cycle
            analysis = await analyze_cycle(self._cycle_id)
            await _db_update_cycle(
                self._cycle_id,
                claude_analysis=analysis.get("reasoning", ""),
                claude_params=json.dumps(analysis),
                confidence_score=analysis["confidence_score"],
            )
            # Phase may have been force-changed while Claude was running; respect it
            if self._phase != "analyze":
                log.info("analyze_phase_overridden", current_phase=self._phase)
                return
            if self._should_go_live(analysis):
                self._active_params = analysis
                self._apply_claude_params(analysis)
                await self._transition_to("deploy")
            else:
                log.info("confidence_too_low",
                         score=analysis["confidence_score"],
                         action="starting_new_learn_cycle")
                self._restore_original_params()
                await _db_update_cycle(self._cycle_id, ended_at=datetime.now(timezone.utc).isoformat())
                await self._start_new_cycle()
        except Exception as e:
            log.error("analyze_failed", error=str(e))
            if self._phase == "analyze":
                await _db_update_cycle(self._cycle_id, ended_at=datetime.now(timezone.utc).isoformat())
                await self._start_new_cycle()
        finally:
            _analyzing = False

    async def _tick_deploy(self) -> None:
        if not self._cycle_id:
            return
        lcfg = CONFIG["learning"]
        count = await _db_get_phase_triggered_count(self._cycle_id, "deploy")
        pnl = await _db_get_phase_pnl(self._cycle_id, "deploy")
        hours = self._phase_hours()
        peg_rate = await _db_recent_peg_cross_rate(self._cycle_id, n=10)
        # Check exit conditions — re-analyze with live data instead of going to validate
        if (count >= lcfg["max_live_trades"]
                or hours >= lcfg["max_live_hours"]
                or pnl < -lcfg["max_live_loss"]
                or (count >= 10 and peg_rate > 0.50)):
            await self._transition_to("analyze")

    async def _tick_validate(self) -> None:
        if not self._cycle_id:
            return
        lcfg = CONFIG["learning"]
        count = await _db_get_phase_triggered_count(self._cycle_id, "validate")
        hours = self._phase_hours()
        if count >= lcfg["min_validate_trades"] or hours >= lcfg["max_validate_hours"]:
            decision = await self._post_validate_decision()
            await _db_update_cycle(self._cycle_id, ended_at=datetime.now(timezone.utc).isoformat())
            if decision == "continue" and self._active_params:
                self._apply_claude_params(self._active_params)
                await self._start_new_cycle()  # new cycle with same params as starting point
            else:
                self._restore_original_params()
                await self._start_new_cycle()

    async def _post_validate_decision(self) -> str:
        deploy_stats = get_phase_stats(self._cycle_id, "deploy")
        val_stats = get_phase_stats(self._cycle_id, "validate")
        if not deploy_stats or not val_stats:
            return "reset"
        live_wr = deploy_stats.get("win_rate") or 0
        val_wr = val_stats.get("win_rate") or 0
        live_pnl = deploy_stats.get("avg_pnl") or 0
        val_pnl = val_stats.get("avg_pnl") or 0
        if val_wr < live_wr - 15 or val_pnl < live_pnl - 0.10:
            log.info("validate_degraded", live_wr=live_wr, val_wr=val_wr)
            return "reset"
        return "continue"

    async def _transition_to(self, new_phase: str) -> None:
        old = self._phase
        self._set_phase(new_phase)
        await _db_update_cycle(
            self._cycle_id,
            phase=new_phase,
            phase_started_at=datetime.now(timezone.utc).isoformat(),
        )
        log.info("phase_transition", cycle=self._cycle_number,
                 from_phase=old, to_phase=new_phase)

    def get_trading_mode(self) -> str:
        """Effective mode for the current phase."""
        if self._phase == "deploy":
            return "live_auto"
        return "paper_auto"

    def get_coin_trigger_threshold(self, coin: str) -> float:
        """Active trigger threshold for a coin (Claude-optimized or config default)."""
        if self._active_params and self._phase in ("deploy", "validate"):
            cp = self._active_params.get("coin_params", {}).get(coin, {})
            if "trigger_threshold" in cp:
                return float(cp["trigger_threshold"])
        return CONFIG["coins"].get(coin, {}).get(
            "trigger_threshold", CONFIG["trading"]["trigger_threshold"]
        )

    def _should_go_live(self, analysis: dict) -> bool:
        if analysis["confidence_score"] < CONFIG["learning"].get("confidence_threshold", 0.60):
            return False
        enabled = sum(
            1 for cp in analysis.get("coin_params", {}).values()
            if cp.get("enabled", True)
        )
        return enabled >= 3

    def _apply_claude_params(self, analysis: dict) -> None:
        self._original_coin_cfg = {}
        for coin, cp in analysis.get("coin_params", {}).items():
            if coin not in CONFIG["coins"]:
                continue
            self._original_coin_cfg[coin] = {
                k: CONFIG["coins"][coin].get(k)
                for k in ("trigger_threshold", "enabled")
            }
            if "trigger_threshold" in cp:
                CONFIG["coins"][coin]["trigger_threshold"] = float(cp["trigger_threshold"])
            if "enabled" in cp:
                CONFIG["coins"][coin]["enabled"] = bool(cp["enabled"])
        gp = analysis.get("global_params", {})
        if "max_entry_cost" in gp:
            CONFIG["entry"]["max_combined_cost"] = float(gp["max_entry_cost"])
        if "max_token_spread" in gp:
            CONFIG["entry"]["max_token_spread"] = float(gp["max_token_spread"])
        log.info("claude_params_applied", cycle=self._cycle_number)

    def _restore_original_params(self) -> None:
        for coin, orig in self._original_coin_cfg.items():
            if coin in CONFIG["coins"]:
                for k, v in orig.items():
                    if v is not None:
                        CONFIG["coins"][coin][k] = v
        self._original_coin_cfg = {}
        log.info("original_params_restored")

    def _current_config_snapshot(self) -> dict:
        return {
            "coins": {
                c: {
                    "trigger_threshold": cfg.get("trigger_threshold", CONFIG["trading"]["trigger_threshold"]),
                    "enabled": cfg.get("enabled", True),
                }
                for c, cfg in CONFIG["coins"].items()
            },
            "exit": dict(CONFIG.get("exit", {})),
            "entry": dict(CONFIG.get("entry", {})),
        }


# ── Apply-learnings helpers (used by live_auto + toggle) ─────────────────────

async def load_and_apply_latest_params() -> bool:
    """Load the most recent completed cycle's claude_params into CONFIG.
    Saves original values so restore_learned_params() can undo the change.
    Returns True when params were found and applied."""
    global _applied_learned_params
    async with _db() as db:
        db.row_factory = __import__("aiosqlite").Row
        async with db.execute(
            "SELECT claude_params, confidence_score, cycle_number FROM learning_cycles "
            "WHERE ended_at IS NOT NULL AND claude_params IS NOT NULL ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    if not row or not row["claude_params"]:
        return False

    params = json.loads(row["claude_params"])

    # Snapshot current values before overwriting
    _applied_learned_params = {
        coin: {k: CONFIG["coins"][coin].get(k) for k in ("trigger_threshold", "enabled")}
        for coin in CONFIG["coins"]
    }
    _applied_learned_params["__entry__"] = {
        "max_combined_cost": CONFIG["entry"].get("max_combined_cost"),
        "max_token_spread": CONFIG["entry"].get("max_token_spread"),
    }

    for coin, cp in params.get("coin_params", {}).items():
        if coin not in CONFIG["coins"]:
            continue
        if "trigger_threshold" in cp:
            CONFIG["coins"][coin]["trigger_threshold"] = float(cp["trigger_threshold"])
        if "enabled" in cp:
            CONFIG["coins"][coin]["enabled"] = bool(cp["enabled"])

    gp = params.get("global_params", {})
    if "max_entry_cost" in gp:
        CONFIG["entry"]["max_combined_cost"] = float(gp["max_entry_cost"])
    if "max_token_spread" in gp:
        CONFIG["entry"]["max_token_spread"] = float(gp["max_token_spread"])

    log.info("learned_params_applied",
             cycle=int(row["cycle_number"]),
             confidence=float(row["confidence_score"] or 0))
    return True


def restore_learned_params() -> None:
    """Undo load_and_apply_latest_params — revert CONFIG to saved originals."""
    global _applied_learned_params
    for key, orig in _applied_learned_params.items():
        if key == "__entry__":
            for k, v in orig.items():
                if v is not None:
                    CONFIG["entry"][k] = v
        elif key in CONFIG["coins"]:
            for k, v in orig.items():
                if v is not None:
                    CONFIG["coins"][key][k] = v
    _applied_learned_params = {}
    log.info("learned_params_restored")


def learned_params_active() -> bool:
    return bool(_applied_learned_params)


# ── Singleton ─────────────────────────────────────────────────────────────────

_orchestrator_instance: Optional[LearningOrchestrator] = None


def get_orchestrator() -> LearningOrchestrator:
    global _orchestrator_instance
    if _orchestrator_instance is None:
        _orchestrator_instance = LearningOrchestrator()
    return _orchestrator_instance
