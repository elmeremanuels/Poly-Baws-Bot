"""Whale Tracker — syncs Polymarket account activity + positions to local SQLite.

Configured addresses are fetched every `sync_interval_secs` seconds (default 300).
Data stored in whale_activity + whale_positions tables, queryable by Claude Code
via `sqlite3 data/trades.db` for comparison with bot trades.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import aiosqlite
import httpx

from .config_loader import CONFIG
from .logger import log, _db_path

# ── Coin extraction ─────────────────────────────────────────────────────────

_KNOWN_COINS = {"BTC", "ETH", "SOL", "DOGE", "XRP", "MATIC", "LINK", "AVAX", "BNB", "ARB", "OP"}


def _extract_coin(question: str) -> str | None:
    if not question:
        return None
    q = question.upper()
    for coin in _KNOWN_COINS:
        if re.search(r"\b" + coin + r"\b", q):
            return coin
    return None


# ── HTTP helpers ─────────────────────────────────────────────────────────────

_BASE = "https://data-api.polymarket.com"


async def fetch_whale_activity(address: str, limit: int = 100) -> list[dict]:
    url = f"{_BASE}/activity?user={address}&limit={limit}"
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("whale_activity_fetch_failed", address=address, error=str(exc))
        return []


async def fetch_whale_positions(address: str, limit: int = 500) -> list[dict]:
    url = f"{_BASE}/positions?user={address}&sizeThreshold=.01&limit={limit}"
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("whale_positions_fetch_failed", address=address, error=str(exc))
        return []


# ── DB write ─────────────────────────────────────────────────────────────────

def _f(item: dict, *keys: str, default: Any = None) -> Any:
    """Try multiple field names, return first found."""
    for k in keys:
        v = item.get(k)
        if v is not None:
            return v
    return default


async def _save_activity(db: aiosqlite.Connection, address: str, name: str, items: list[dict]) -> int:
    inserted = 0
    for item in items:
        tx_hash = _f(item, "transactionHash", "id", default="")
        if not tx_hash:
            continue
        question = _f(item, "title", "question", "market_question", default="")
        coin = _extract_coin(question)
        # side: outcome (YES/NO), type: BUY/SELL
        outcome_side = _f(item, "outcome", default="")
        if not outcome_side:
            idx = item.get("outcomeIndex")
            outcome_side = "YES" if idx == 0 else ("NO" if idx == 1 else "")
        trade_type = _f(item, "type", "side", default="BUY")
        price = float(_f(item, "price", default=0) or 0)
        size = float(_f(item, "size", default=0) or 0)
        usdc = float(_f(item, "usdcSize", "amount", default=0) or 0)
        market_id = _f(item, "market", "conditionId", "marketId", default="")
        ts = _f(item, "timestamp", "createdAt", "created_at", default="")

        try:
            await db.execute(
                """INSERT OR IGNORE INTO whale_activity
                   (address, name, transaction_hash, market_id, question, outcome_side,
                    trade_type, price, size, usdc_size, coin, event_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (address, name, tx_hash, market_id, question, outcome_side,
                 trade_type, price, size, usdc, coin, ts),
            )
            inserted += 1
        except Exception:
            pass
    return inserted


async def _save_positions(db: aiosqlite.Connection, address: str, name: str, items: list[dict]) -> None:
    await db.execute("DELETE FROM whale_positions WHERE address = ?", (address,))
    for pos in items:
        question = _f(pos, "title", "question", default="")
        coin = _extract_coin(question)
        condition_id = _f(pos, "conditionId", "market", "marketId", default="")
        side = _f(pos, "outcome", default="")
        if not side:
            idx = pos.get("outcomeIndex")
            side = "YES" if idx == 0 else ("NO" if idx == 1 else "")
        size = float(_f(pos, "size", default=0) or 0)
        avg_price = float(_f(pos, "avgPrice", "averagePrice", default=0) or 0)
        cur_price = float(_f(pos, "curPrice", "currentPrice", default=0) or 0)
        cash_pnl = float(_f(pos, "cashPnl", "pnl", default=0) or 0)
        pct_pnl = float(_f(pos, "percentPnl", "pctPnl", default=0) or 0)
        redeemable = int(bool(_f(pos, "redeemable", "canRedeem", default=False)))

        try:
            await db.execute(
                """INSERT INTO whale_positions
                   (address, name, condition_id, question, side, size,
                    avg_price, cur_price, cash_pnl, pct_pnl, is_redeemable, coin)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (address, name, condition_id, question, side, size,
                 avg_price, cur_price, cash_pnl, pct_pnl, redeemable, coin),
            )
        except Exception:
            pass


async def fetch_whale_activity_paginated(address: str, max_records: int = 5000) -> list[dict]:
    """Paginate through all available activity. Returns up to max_records items."""
    all_items: list[dict] = []
    page_size = 100
    offset = 0
    async with httpx.AsyncClient(timeout=15) as client:
        while len(all_items) < max_records:
            url = f"{_BASE}/activity?user={address}&limit={page_size}&offset={offset}"
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                page = resp.json()
                if not page or not isinstance(page, list):
                    break
                all_items.extend(page)
                if len(page) < page_size:
                    break
                offset += page_size
                await asyncio.sleep(0.25)  # rate-limit friendly
            except Exception as exc:
                log.warning("whale_history_page_failed", address=address, offset=offset, error=str(exc))
                break
    return all_items


# ── Sync (blocking) versions for use from Streamlit threads ──────────────────

def fetch_whale_activity_paginated_sync(address: str, max_records: int = 5000) -> list[dict]:
    """Blocking paginated fetch — safe to call from Streamlit button handlers."""
    import time as _time
    all_items: list[dict] = []
    page_size = 100
    offset = 0
    with httpx.Client(timeout=15) as client:
        while len(all_items) < max_records:
            url = f"{_BASE}/activity?user={address}&limit={page_size}&offset={offset}"
            try:
                resp = client.get(url)
                resp.raise_for_status()
                page = resp.json()
                if not page or not isinstance(page, list):
                    break
                all_items.extend(page)
                if len(page) < page_size:
                    break
                offset += page_size
                _time.sleep(0.25)
            except Exception as exc:
                log.warning("whale_history_page_failed", address=address, offset=offset, error=str(exc))
                break
    return all_items


def deep_sync_whale_sync(name: str, address: str) -> tuple[int, int]:
    """Full historical sync, blocking. Returns (total_fetched, new_rows)."""
    import sqlite3 as _sqlite3

    all_activity = fetch_whale_activity_paginated_sync(address)

    conn = _sqlite3.connect(str(_db_path), timeout=30)
    conn.row_factory = _sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        inserted = 0
        for item in all_activity:
            tx_hash = _f(item, "transactionHash", "id", default="")
            if not tx_hash:
                continue
            question = _f(item, "title", "question", "market_question", default="")
            coin = _extract_coin(question)
            outcome_side = _f(item, "outcome", default="")
            if not outcome_side:
                idx = item.get("outcomeIndex")
                outcome_side = "YES" if idx == 0 else ("NO" if idx == 1 else "")
            trade_type = _f(item, "type", "side", default="BUY")
            price = float(_f(item, "price", default=0) or 0)
            size = float(_f(item, "size", default=0) or 0)
            usdc = float(_f(item, "usdcSize", "amount", default=0) or 0)
            market_id = _f(item, "market", "conditionId", "marketId", default="")
            ts = _f(item, "timestamp", "createdAt", "created_at", default="")
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO whale_activity
                       (address, name, transaction_hash, market_id, question, outcome_side,
                        trade_type, price, size, usdc_size, coin, event_ts)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (address, name, tx_hash, market_id, question, outcome_side,
                     trade_type, price, size, usdc, coin, ts),
                )
                inserted += 1
            except Exception:
                pass
        conn.execute(
            "INSERT OR REPLACE INTO whale_meta "
            "(address, name, last_synced_at, activity_count, positions_count, history_loaded) "
            "SELECT ?, ?, datetime('now'), "
            "(SELECT COUNT(*) FROM whale_activity WHERE address=?), "
            "COALESCE((SELECT positions_count FROM whale_meta WHERE address=?), 0), 1",
            (address, name, address, address),
        )
        conn.commit()
    finally:
        conn.close()

    log.info("whale_deep_sync_done", name=name, total=len(all_activity), new_rows=inserted)
    return len(all_activity), inserted


# ── Public sync entry point ──────────────────────────────────────────────────

async def sync_whale(name: str, address: str) -> None:
    activity = await fetch_whale_activity(address)
    positions = await fetch_whale_positions(address)

    async with aiosqlite.connect(_db_path, timeout=10) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        new_rows = await _save_activity(db, address, name, activity)
        await _save_positions(db, address, name, positions)
        await db.execute(
            "INSERT OR REPLACE INTO whale_meta (address, name, last_synced_at, activity_count, positions_count) "
            "VALUES (?, ?, datetime('now'), ?, ?)",
            (address, name, len(activity), len(positions)),
        )
        await db.commit()

    log.info(
        "whale_synced",
        name=name,
        new_activity=new_rows,
        positions=len(positions),
    )


async def whale_sync_loop() -> None:
    cfg = CONFIG.get("whale_tracker", {})
    if not cfg.get("enabled", False):
        return
    interval = int(cfg.get("sync_interval_secs", 300))
    addresses: dict[str, str] = cfg.get("addresses", {})
    if not addresses:
        log.info("whale_tracker_no_addresses_configured")
        return

    log.info("whale_sync_loop_started", addresses=list(addresses.keys()), interval=interval)
    # Initial sync immediately on startup
    for name, address in addresses.items():
        try:
            await sync_whale(name, address)
        except Exception as exc:
            log.warning("whale_sync_error", name=name, error=str(exc))

    while True:
        await asyncio.sleep(interval)
        for name, address in addresses.items():
            try:
                await sync_whale(name, address)
            except Exception as exc:
                log.warning("whale_sync_error", name=name, error=str(exc))
