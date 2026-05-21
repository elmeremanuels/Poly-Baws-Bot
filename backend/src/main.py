"""FastAPI app entrypoint + bot orchestration startup."""
import asyncio
import argparse
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .logger import init_db, log
from .state import recover_state, set_mode
from .config_loader import CONFIG
from . import risk, bot as bot_module
from .api import router, broadcast, push_state_update


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    log.info("startup_begin")
    await init_db()

    # Pre-flight checks
    if risk.is_killed():
        log.critical("kill_flag_present_on_startup")
        yield
        return

    await recover_state()

    # Restore persisted mode if available
    from .logger import load_dashboard_state
    saved_mode = await load_dashboard_state("mode")
    if saved_mode:
        try:
            set_mode(saved_mode)
        except ValueError:
            pass

    bot_module.set_broadcast_fn(push_state_update)

    bot_task = asyncio.create_task(bot_module.run_bot())

    log.info("startup_complete")
    yield

    # Shutdown
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass
    log.info("shutdown_complete")


app = FastAPI(title="Poly-Baws-Bot", lifespan=lifespan)
app.include_router(router)

# Serve built React frontend
_FRONTEND_DIST = Path(__file__).parent.parent.parent / "frontend" / "dist"
if _FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=_FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        index = _FRONTEND_DIST / "index.html"
        return FileResponse(index)


def cli():
    parser = argparse.ArgumentParser(description="Poly-Baws-Bot")
    parser.add_argument("--kill", action="store_true", help="Activate kill switch and exit")
    parser.add_argument("--reset-kill", action="store_true", help="Reset kill switch")
    args = parser.parse_args()

    if args.kill:
        risk.kill("cli")
        print("Kill switch activated.")
        sys.exit(0)

    if args.reset_kill:
        risk.reset_kill()
        print("Kill switch reset.")
        sys.exit(0)

    import uvicorn
    uvicorn.run(
        "src.main:app",
        host=CONFIG["dashboard"]["host"],
        port=CONFIG["dashboard"]["port"],
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    cli()
