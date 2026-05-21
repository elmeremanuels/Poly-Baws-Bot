"""FastAPI REST endpoints and WebSocket for the dashboard."""
import asyncio
import json
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Depends, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from .config_loader import CONFIG, get_env
from .logger import (
    log, get_daily_pnl, get_today_trade_count, get_recent_trades,
    get_open_trades, get_events_for_trade, save_dashboard_state, load_dashboard_state,
    get_scanner_alerts,
)
from . import risk, scanner, ws_client
from .state import (
    get_mode, set_mode, get_active_trades, get_active_count_by_coin,
)
from . import bot as bot_module

router = APIRouter()
security = HTTPBasic()
_ws_clients: set[WebSocket] = set()


def check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    user = CONFIG["dashboard"]["auth_user"]
    passwd = get_env("DASHBOARD_AUTH_PASS", "changeme")
    ok_user = secrets.compare_digest(credentials.username.encode(), user.encode())
    ok_pass = secrets.compare_digest(credentials.password.encode(), passwd.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


async def broadcast(data: dict) -> None:
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


# ── WebSocket ────────────────────────────────────────────────────────────────

@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        # Send initial state
        await _send_full_state(ws)
        while True:
            # Keep alive; client can send pings
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


async def _send_full_state(ws: WebSocket) -> None:
    state = await _build_full_state()
    await ws.send_text(json.dumps({"event": "full_state", **state}))


async def _build_full_state() -> dict:
    daily_pnl = await get_daily_pnl()
    active_trades = list(get_active_trades().values())
    recent_trades = await get_recent_trades(20)
    counts = get_active_count_by_coin()

    coin_data = {}
    for coin in CONFIG["coins"]:
        coin_data[coin] = {
            "enabled": CONFIG["coins"][coin]["enabled"],
            "max_parallel": CONFIG["coins"][coin]["max_parallel_positions"],
            "active_positions": counts.get(coin, 0),
            "today_pnl": await get_daily_pnl(coin),
            "today_trades": await get_today_trade_count(coin),
            "next_windows": [
                {
                    "market_id": m.get("market_id"),
                    "window_start": m["window_start"].isoformat() if m["window_start"] else None,
                    "question": m.get("question", ""),
                }
                for m in scanner.get_next_windows(coin, 3)
            ],
        }

    return {
        "mode": get_mode(),
        "killed": risk.is_killed(),
        "kill_reason": risk.get_kill_reason(),
        "ws_connected": ws_client.is_connected(),
        "daily_pnl": daily_pnl,
        "active_trades": active_trades,
        "recent_trades": recent_trades,
        "coins": coin_data,
        "hybrid_pending": [
            k for k in bot_module.get_hybrid_pending().keys()
        ],
        "scanner_alerts": await get_scanner_alerts(),
    }


async def push_state_update() -> None:
    """Broadcast updated state to all WS clients."""
    state = await _build_full_state()
    await broadcast({"event": "state_update", **state})


# ── REST endpoints ────────────────────────────────────────────────────────────

@router.get("/api/status")
async def get_status(user=Depends(check_auth)):
    return await _build_full_state()


@router.post("/api/mode")
async def set_trading_mode(payload: dict, user=Depends(check_auth)):
    mode = payload.get("mode")
    try:
        set_mode(mode)
        await save_dashboard_state("mode", mode)
        await push_state_update()
        return {"ok": True, "mode": mode}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/kill")
async def activate_kill_switch(user=Depends(check_auth)):
    risk.kill("dashboard_user")
    await push_state_update()
    return {"ok": True, "killed": True}


@router.post("/api/kill/reset")
async def reset_kill_switch(user=Depends(check_auth)):
    risk.reset_kill()
    await push_state_update()
    return {"ok": True, "killed": False}


@router.post("/api/hybrid/trigger/{market_id}")
async def trigger_hybrid(market_id: str, user=Depends(check_auth)):
    ok = await bot_module.trigger_hybrid_entry(market_id)
    if not ok:
        raise HTTPException(404, f"No pending hybrid window for market_id={market_id}")
    await push_state_update()
    return {"ok": True, "market_id": market_id}


@router.get("/api/trades")
async def list_trades(limit: int = 20, user=Depends(check_auth)):
    trades = await get_recent_trades(limit)
    return {"trades": trades}


@router.get("/api/trades/{trade_id}/events")
async def trade_events(trade_id: str, user=Depends(check_auth)):
    events = await get_events_for_trade(trade_id)
    return {"events": events}


@router.get("/api/positions")
async def list_positions(user=Depends(check_auth)):
    active = list(get_active_trades().values())
    return {"positions": active}


class CoinConfig(BaseModel):
    enabled: bool | None = None
    max_parallel_positions: int | None = None


@router.post("/api/coins/{coin}/config")
async def update_coin_config(coin: str, config: CoinConfig, user=Depends(check_auth)):
    if coin not in CONFIG["coins"]:
        raise HTTPException(404, f"Unknown coin: {coin}")
    if config.enabled is not None:
        CONFIG["coins"][coin]["enabled"] = config.enabled
    if config.max_parallel_positions is not None:
        CONFIG["coins"][coin]["max_parallel_positions"] = config.max_parallel_positions
    await save_dashboard_state(f"coin_{coin}_enabled", str(CONFIG["coins"][coin]["enabled"]))
    await save_dashboard_state(f"coin_{coin}_max", str(CONFIG["coins"][coin]["max_parallel_positions"]))
    await push_state_update()
    return {"ok": True, "coin": coin, "config": CONFIG["coins"][coin]}


@router.get("/api/pnl/summary")
async def pnl_summary(user=Depends(check_auth)):
    result = {}
    for coin in CONFIG["coins"]:
        result[coin] = {
            "today": await get_daily_pnl(coin),
            "trade_count": await get_today_trade_count(coin),
        }
    result["total"] = {
        "today": await get_daily_pnl(),
        "trade_count": await get_today_trade_count(),
    }
    return result
