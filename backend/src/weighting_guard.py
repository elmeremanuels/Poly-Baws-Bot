"""
weighting_guard.py — Auto-manages conviction_weighting and directional_entry.

Runs every `weighting_guard.eval_interval_secs` (default 30 min).
Queries the last N closed trades and checks whether the confidence-driven
direction prediction matches the actual winner.

Decision table:
  direction_accuracy < disable_threshold  → disable the feature
  direction_accuracy >= enable_threshold  → re-enable (if auto_reenable: true)
  not enough trades yet                   → no change

A feature that was manually disabled via the dashboard (set via set_conviction_weighting
or set_directional_entry commands) will NOT be auto-re-enabled regardless of the flag
— the guard only re-enables features it previously auto-disabled.
"""
import asyncio

import aiosqlite

from .config_loader import CONFIG
from .logger import _db_path, log, save_dashboard_state

_STATE_KEY_CW = "wg_cw_auto_disabled"      # conviction_weighting
_STATE_KEY_DE = "wg_de_auto_disabled"      # directional_entry
_STATE_KEY_CW_RATE = "wg_cw_last_rate"
_STATE_KEY_DE_RATE = "wg_de_last_rate"


async def weighting_guard_loop() -> None:
    """Periodic evaluation loop. Spawned by bot.py on startup."""
    cfg = CONFIG.get("weighting_guard", {})
    if not cfg.get("enabled", True):
        log.info("weighting_guard_disabled_by_config")
        return

    interval = int(cfg.get("eval_interval_secs", 1800))
    await asyncio.sleep(120)  # give the rest of the bot time to start up

    while True:
        try:
            await _evaluate_and_adjust()
        except Exception as exc:
            log.error("weighting_guard_error", error=str(exc))
        await asyncio.sleep(interval)


async def _evaluate_and_adjust() -> None:
    cfg = CONFIG.get("weighting_guard", {})
    min_trades = int(cfg.get("min_trades", 15))
    disable_thr = float(cfg.get("disable_threshold", 0.47))
    enable_thr = float(cfg.get("enable_threshold", 0.53))
    auto_reenable = bool(cfg.get("auto_reenable", True))
    window = max(min_trades * 2, 30)

    async with aiosqlite.connect(str(_db_path), timeout=10.0) as db:
        db.row_factory = aiosqlite.Row

        # ── Directional entry ─────────────────────────────────────────────────
        async with db.execute(
            """SELECT directional_side, actual_winner
               FROM trades
               WHERE entry_type = 'directional'
                 AND status IN ('closed', 'resolved')
                 AND actual_winner IS NOT NULL
                 AND directional_side IS NOT NULL
               ORDER BY created_at DESC LIMIT ?""",
            (window,),
        ) as cur:
            de_trades = await cur.fetchall()

        if len(de_trades) >= min_trades:
            correct = sum(1 for t in de_trades if t["directional_side"] == t["actual_winner"])
            rate = round(correct / len(de_trades), 3)
            await save_dashboard_state(_STATE_KEY_DE_RATE, str(rate))

            de_cfg = CONFIG.setdefault("directional_entry", {})
            cur_enabled = de_cfg.get("enabled", False)

            if rate < disable_thr and cur_enabled:
                de_cfg["enabled"] = False
                await save_dashboard_state(_STATE_KEY_DE, "true")
                log.warning(
                    "weighting_guard_auto_disabled_directional",
                    accuracy=rate, n=len(de_trades), threshold=disable_thr,
                )
            elif rate >= enable_thr and not cur_enabled and auto_reenable:
                # Only re-enable if the guard itself disabled it (not a manual override)
                from .logger import load_dashboard_state
                guard_disabled = await load_dashboard_state(_STATE_KEY_DE)
                if guard_disabled == "true":
                    de_cfg["enabled"] = True
                    await save_dashboard_state(_STATE_KEY_DE, "false")
                    log.info(
                        "weighting_guard_auto_enabled_directional",
                        accuracy=rate, n=len(de_trades), threshold=enable_thr,
                    )
            else:
                log.info(
                    "weighting_guard_directional_ok",
                    accuracy=rate, n=len(de_trades), enabled=cur_enabled,
                )

        # ── Conviction weighting ──────────────────────────────────────────────
        async with db.execute(
            """SELECT bias_direction_at_entry, actual_winner
               FROM trades
               WHERE entry_type = 'straddle'
                 AND yes_size != no_size
                 AND bias_direction_at_entry IS NOT NULL
                 AND actual_winner IS NOT NULL
                 AND status IN ('closed', 'resolved')
               ORDER BY created_at DESC LIMIT ?""",
            (window,),
        ) as cur:
            cw_trades = await cur.fetchall()

        if len(cw_trades) >= min_trades:
            correct = sum(
                1 for t in cw_trades
                if (t["bias_direction_at_entry"] == "UP" and t["actual_winner"] == "YES")
                or (t["bias_direction_at_entry"] == "DOWN" and t["actual_winner"] == "NO")
            )
            rate = round(correct / len(cw_trades), 3)
            await save_dashboard_state(_STATE_KEY_CW_RATE, str(rate))

            cw_cfg = CONFIG.setdefault("conviction_weighting", {})
            cur_enabled = cw_cfg.get("enabled", False)

            if rate < disable_thr and cur_enabled:
                cw_cfg["enabled"] = False
                await save_dashboard_state(_STATE_KEY_CW, "true")
                log.warning(
                    "weighting_guard_auto_disabled_conviction_weighting",
                    accuracy=rate, n=len(cw_trades), threshold=disable_thr,
                )
            elif rate >= enable_thr and not cur_enabled and auto_reenable:
                from .logger import load_dashboard_state
                guard_disabled = await load_dashboard_state(_STATE_KEY_CW)
                if guard_disabled == "true":
                    cw_cfg["enabled"] = True
                    await save_dashboard_state(_STATE_KEY_CW, "false")
                    log.info(
                        "weighting_guard_auto_enabled_conviction_weighting",
                        accuracy=rate, n=len(cw_trades), threshold=enable_thr,
                    )
            else:
                log.info(
                    "weighting_guard_conviction_weighting_ok",
                    accuracy=rate, n=len(cw_trades), enabled=cur_enabled,
                )


async def get_guard_status() -> dict:
    """Return current guard stats for the dashboard."""
    from .logger import load_dashboard_state
    de_rate_str = await load_dashboard_state(_STATE_KEY_DE_RATE)
    cw_rate_str = await load_dashboard_state(_STATE_KEY_CW_RATE)
    de_auto_disabled = await load_dashboard_state(_STATE_KEY_DE)
    cw_auto_disabled = await load_dashboard_state(_STATE_KEY_CW)

    de_cfg = CONFIG.get("directional_entry", {})
    cw_cfg = CONFIG.get("conviction_weighting", {})
    guard_cfg = CONFIG.get("weighting_guard", {})

    return {
        "guard_enabled": guard_cfg.get("enabled", True),
        "disable_threshold": guard_cfg.get("disable_threshold", 0.47),
        "enable_threshold": guard_cfg.get("enable_threshold", 0.53),
        "auto_reenable": guard_cfg.get("auto_reenable", True),
        "directional": {
            "enabled": de_cfg.get("enabled", False),
            "auto_disabled": de_auto_disabled == "true",
            "last_accuracy": float(de_rate_str) if de_rate_str else None,
        },
        "conviction_weighting": {
            "enabled": cw_cfg.get("enabled", False),
            "auto_disabled": cw_auto_disabled == "true",
            "last_accuracy": float(cw_rate_str) if cw_rate_str else None,
        },
    }
