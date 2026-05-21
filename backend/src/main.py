"""Bot process entry point — no HTTP server, no WebSocket push."""
import asyncio
import argparse
import sys

from .logger import init_db, log
from .state import recover_state, set_mode
from .logger import load_dashboard_state
from .config_loader import CONFIG
from . import risk
from . import bot as bot_module
from .commands import command_poll_loop


async def _run() -> None:
    log.info("startup_begin")
    await init_db()

    if risk.is_killed():
        log.critical("kill_flag_present_on_startup_aborting")
        return

    await recover_state()

    saved_mode = await load_dashboard_state("mode")
    if saved_mode:
        try:
            set_mode(saved_mode)
        except ValueError:
            pass

    log.info("startup_complete", mode=risk.get_kill_reason() or "ok")

    await asyncio.gather(
        bot_module.run_bot(),
        command_poll_loop(),
    )


def cli() -> None:
    parser = argparse.ArgumentParser(description="Poly-Baws-Bot")
    parser.add_argument("--kill", action="store_true")
    parser.add_argument("--reset-kill", action="store_true")
    args = parser.parse_args()

    if args.kill:
        risk.kill("cli")
        print("Kill switch activated.")
        sys.exit(0)
    if args.reset_kill:
        risk.reset_kill()
        print("Kill switch reset.")
        sys.exit(0)

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
