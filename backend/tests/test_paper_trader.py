"""Unit tests for paper trading simulation."""
import pytest
import asyncio
from unittest.mock import patch

# Minimal orderbook fixture
MOCK_BOOK = {
    "bids": {"0.48": 10.0, "0.45": 20.0},
    "asks": {"0.50": 5.0, "0.52": 15.0},
}


@pytest.fixture(autouse=True)
def patch_ws(monkeypatch):
    import src.ws_client as ws
    monkeypatch.setattr(ws, "get_orderbook", lambda _: MOCK_BOOK)
    monkeypatch.setattr(ws, "get_best_bid", lambda _: 0.48)
    monkeypatch.setattr(ws, "get_best_ask", lambda _: 0.50)
    monkeypatch.setattr(ws, "get_mid_price", lambda _: 0.49)


@pytest.mark.asyncio
async def test_limit_buy_fills_at_ask():
    from src.paper_trader import simulate_limit_buy
    result = await simulate_limit_buy("token", 0.50, 5.0)
    assert result["filled"] is True
    assert result["fill_price"] == pytest.approx(0.50, abs=0.01)
    assert result["filled_size"] == 5.0


@pytest.mark.asyncio
async def test_limit_buy_no_fill_above_ask():
    from src.paper_trader import simulate_limit_buy
    result = await simulate_limit_buy("token", 0.40, 5.0)
    # 0.40 limit < 0.50 ask (minus slippage) => no fill
    assert result["filled"] is False


@pytest.mark.asyncio
async def test_market_sell_fills():
    from src.paper_trader import simulate_market_sell
    result = await simulate_market_sell("token", 5.0)
    assert result["filled"] is True
    assert result["fill_price"] == pytest.approx(0.48, abs=0.01)


@pytest.mark.asyncio
async def test_limit_sell_above_bid_no_fill():
    from src.paper_trader import simulate_limit_sell
    result = await simulate_limit_sell("token", 0.70, 5.0)
    assert result["filled"] is False


def test_check_trigger_no_trigger():
    from src.paper_trader import check_trigger
    winner, price = check_trigger("y", "n", 0.70)
    assert winner is None


def test_check_trigger_yes_wins():
    import src.ws_client as ws
    import src.paper_trader as pt
    original = ws.get_mid_price
    ws.get_mid_price = lambda t: 0.72 if t == "yes_tok" else 0.28
    winner, price = pt.check_trigger("yes_tok", "no_tok", 0.70)
    ws.get_mid_price = original
    assert winner == "YES"
    assert price == pytest.approx(0.72)
