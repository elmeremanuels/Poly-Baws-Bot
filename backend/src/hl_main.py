"""Entry point voor de Hyperliquid trader — start via `python -m src.hl_main`."""
import asyncio

from .config_loader import CONFIG
from .logger import log
from .hyperliquid_trader import hl_multi_loop


def main() -> None:
    cfg = CONFIG.get("hyperliquid_trader", {})
    if not cfg.get("enabled", False):
        log.info("hl_main_disabled", reason="hyperliquid_trader.enabled is false in config.yaml")
        return
    paper = cfg.get("paper_mode", True)
    log.info("hl_main_start", paper=paper)
    asyncio.run(hl_multi_loop(paper=paper))


if __name__ == "__main__":
    main()
