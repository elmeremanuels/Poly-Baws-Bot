"""Download 3 months of Binance 1-min klines + funding rate history.

Stores everything in backend/data/historical.db — used for backtesting
OFI / funding-rate signal quality and tuning conviction thresholds.

Tables created:
  klines       — 1-min OHLCV + taker_buy_base_volume (OFI proxy)
  funding_rate — 8-hourly perpetual funding rate history

Usage:
  python -m backend.scripts.download_historical_data
  python -m backend.scripts.download_historical_data --days 90
  python -m backend.scripts.download_historical_data --coins BTC ETH
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import time
from pathlib import Path

import httpx

COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
_COIN_TO_SYMBOL: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
    "DOGE": "DOGEUSDT",
}

_REST_BASE = "https://api.binance.com"
_FAPI_BASE = "https://fapi.binance.com"
_DB_PATH = Path(__file__).parent.parent / "data" / "historical.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS klines (
    coin        TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    open_time   INTEGER NOT NULL,       -- unix ms
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,                  -- base asset volume
    close_time  INTEGER,
    quote_volume REAL,
    trade_count INTEGER,
    taker_buy_base_volume REAL,        -- OFI proxy: taker buys (index 9)
    taker_buy_quote_volume REAL,
    PRIMARY KEY (coin, open_time)
);

CREATE TABLE IF NOT EXISTS funding_rate (
    coin            TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    funding_time    INTEGER NOT NULL,  -- unix ms
    funding_rate    REAL,
    PRIMARY KEY (coin, funding_time)
);

CREATE INDEX IF NOT EXISTS idx_klines_coin_time ON klines(coin, open_time);
CREATE INDEX IF NOT EXISTS idx_fr_coin_time ON funding_rate(coin, funding_time);
"""


def _init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


async def _fetch_klines(
    client: httpx.AsyncClient, symbol: str, start_ms: int, end_ms: int
) -> list[list]:
    """Fetch all 1-min klines between start_ms and end_ms (handles pagination)."""
    all_rows: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        resp = await client.get(
            f"{_REST_BASE}/api/v3/klines",
            params={
                "symbol": symbol,
                "interval": "1m",
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        all_rows.extend(rows)
        cursor = int(rows[-1][6]) + 1  # close_time of last candle + 1ms
        await asyncio.sleep(0.08)  # ~12 req/s, well within Binance limit
    return all_rows


async def _fetch_funding_rates(
    client: httpx.AsyncClient, symbol: str, start_ms: int, end_ms: int
) -> list[dict]:
    """Fetch all 8-hourly funding rate records between start_ms and end_ms."""
    all_rows: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        resp = await client.get(
            f"{_FAPI_BASE}/fapi/v1/fundingRate",
            params={
                "symbol": symbol,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        all_rows.extend(rows)
        cursor = int(rows[-1]["fundingTime"]) + 1
        await asyncio.sleep(0.08)
    return all_rows


def _upsert_klines(conn: sqlite3.Connection, coin: str, symbol: str, rows: list[list]) -> int:
    sql = """
        INSERT OR REPLACE INTO klines
          (coin, symbol, open_time, open, high, low, close, volume,
           close_time, quote_volume, trade_count,
           taker_buy_base_volume, taker_buy_quote_volume)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    records = [
        (
            coin, symbol,
            int(r[0]),   # open_time ms
            float(r[1]), float(r[2]), float(r[3]), float(r[4]),  # ohlc
            float(r[5]), # volume
            int(r[6]),   # close_time
            float(r[7]), # quote_volume
            int(r[8]),   # trade_count
            float(r[9]), # taker_buy_base_volume  ← OFI proxy
            float(r[10]),# taker_buy_quote_volume
        )
        for r in rows
    ]
    conn.executemany(sql, records)
    conn.commit()
    return len(records)


def _upsert_funding(conn: sqlite3.Connection, coin: str, symbol: str, rows: list[dict]) -> int:
    sql = """
        INSERT OR REPLACE INTO funding_rate (coin, symbol, funding_time, funding_rate)
        VALUES (?,?,?,?)
    """
    records = [
        (coin, symbol, int(r["fundingTime"]), float(r["fundingRate"]))
        for r in rows
    ]
    conn.executemany(sql, records)
    conn.commit()
    return len(records)


async def download(coins: list[str], days: int, db_path: Path) -> None:
    conn = _init_db(db_path)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 60 * 60 * 1000

    print(f"Downloading {days} days of data for {coins} → {db_path}")
    print(f"Period: {days} days ({len(coins)} coins)")

    async with httpx.AsyncClient(timeout=30) as client:
        for coin in coins:
            if coin not in _COIN_TO_SYMBOL:
                print(f"  SKIP {coin}: no symbol mapping")
                continue
            symbol = _COIN_TO_SYMBOL[coin]
            perp_symbol = symbol  # USDT perps use same symbol on fapi

            print(f"  [{coin}] fetching klines ...", end=" ", flush=True)
            try:
                klines = await _fetch_klines(client, symbol, start_ms, end_ms)
                n = _upsert_klines(conn, coin, symbol, klines)
                print(f"{n} rows saved")
            except Exception as e:
                print(f"ERROR: {e}")

            print(f"  [{coin}] fetching funding rates ...", end=" ", flush=True)
            try:
                fr_rows = await _fetch_funding_rates(client, perp_symbol, start_ms, end_ms)
                n = _upsert_funding(conn, coin, perp_symbol, fr_rows)
                print(f"{n} rows saved")
            except Exception as e:
                print(f"ERROR: {e}")

    conn.close()
    print("Done.")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download Binance historical data for backtesting")
    p.add_argument("--days", type=int, default=90, help="Days of history to download (default: 90)")
    p.add_argument("--coins", nargs="+", default=COINS, help="Coins to download (default: all)")
    p.add_argument("--db", default=str(_DB_PATH), help="Output SQLite path")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(download(args.coins, args.days, Path(args.db)))
