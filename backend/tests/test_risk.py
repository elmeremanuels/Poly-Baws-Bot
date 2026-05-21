"""Unit tests for risk module."""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch


def test_kill_and_reset():
    from src.risk import kill, reset_kill, is_killed
    kill("test")
    assert is_killed()
    reset_kill()
    assert not is_killed()


@pytest.mark.asyncio
async def test_pre_trade_checks_killed():
    from src import risk
    from src.risk import kill, reset_kill, pre_trade_checks
    kill("test")
    ok, reason = await pre_trade_checks("BTC", {})
    assert not ok
    assert "kill_switch" in reason
    reset_kill()


@pytest.mark.asyncio
async def test_pre_trade_checks_coin_disabled():
    from src.config_loader import CONFIG
    from src.risk import pre_trade_checks
    CONFIG["coins"]["BTC"]["enabled"] = False
    ok, reason = await pre_trade_checks("BTC", {})
    CONFIG["coins"]["BTC"]["enabled"] = True
    assert not ok
    assert "coin_disabled" in reason


@pytest.mark.asyncio
async def test_pre_trade_checks_position_limit():
    from src.risk import pre_trade_checks
    from src.config_loader import CONFIG
    max_p = CONFIG["coins"]["ETH"]["max_parallel_positions"]
    ok, reason = await pre_trade_checks("ETH", {"ETH": max_p})
    assert not ok
    assert "coin_position_limit" in reason
