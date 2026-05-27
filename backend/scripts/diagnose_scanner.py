#!/usr/bin/env python3
"""
Diagnoseert waarom ETH/SOL (of andere munten) niet gevonden worden door de scanner.

Stap 1: test de Gamma API direct (geen bot nodig)
Stap 2: laat zien welke slugs er terugkomen per coin-filter
Stap 3: laat zien of normalisatie werkt
Stap 4: vergelijkt met wat de scanner in _market_cache heeft zitten

Gebruik:
  venv/bin/python3 backend/scripts/diagnose_scanner.py
"""
import asyncio, json, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Voeg de backend/src directory toe aan het pad
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

GAMMA_URL = "https://gamma-api.polymarket.com"

# Coin → slug-filter (zelfde als in config.yaml)
FILTERS = {
    "BTC":  "btc-updown-5m",
    "ETH":  "eth-updown-5m",
    "SOL":  "sol-updown-5m",
    "XRP":  "xrp-updown-5m",
    "DOGE": "doge-updown-5m",
}

SEP = "─" * 72


async def fetch_events() -> list[dict]:
    now = datetime.now(timezone.utc)
    end_max = (now + timedelta(hours=26)).isoformat()
    params = {
        "closed": "false",
        "limit": 2000,
        "order": "endDate",
        "ascending": "true",
        "end_date_min": now.isoformat(),
        "end_date_max": end_max,
    }
    print(f"Gamma API query:")
    print(f"  end_date_min = {now.isoformat()[:19]} UTC")
    print(f"  end_date_max = {(now + timedelta(hours=26)).isoformat()[:19]} UTC")
    print(f"  limit        = 2000")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{GAMMA_URL}/events", params=params)
        print(f"  HTTP status  = {resp.status_code}")
        if resp.status_code != 200:
            print(f"  FOUT: onverwachte statuscode. Body: {resp.text[:200]}")
            return []
        events = resp.json()
        if not isinstance(events, list):
            events = events.get("events", [])
        return events
    except Exception as e:
        print(f"  FOUT bij ophalen: {e}")
        return []


def check_coin(coin: str, f: str, events: list[dict]) -> None:
    now = datetime.now(timezone.utc)
    matched = []
    for event in events:
        slug = (event.get("slug") or "").lower()
        if f.lower() in slug:
            matched.append(event)

    print(f"\n{'═'*72}")
    print(f"  {coin}  (filter: '{f}')")
    print(f"{'═'*72}")
    print(f"  Gevonden events: {len(matched)}")

    if not matched:
        # Toon wat er WEL in de response zit voor debug
        all_slugs = [(e.get("slug") or "")[:50] for e in events[:20]]
        print(f"  ⚠️  GEEN events gevonden voor filter '{f}'")
        print(f"  Eerste 20 slugs in API-response:")
        for s in all_slugs:
            print(f"    {s}")
        return

    # Toon de eerste 3 gevonden events
    ok_count = 0
    skip_clob = 0
    skip_window = 0
    for event in matched:
        for market in event.get("markets") or []:
            clob_tokens = market.get("clobTokenIds")
            if isinstance(clob_tokens, str):
                try:
                    clob_tokens = json.loads(clob_tokens)
                except Exception:
                    clob_tokens = None

            if not clob_tokens or len(clob_tokens) < 2:
                skip_clob += 1
                continue

            slug = event.get("slug", "")
            parts = slug.rsplit("-", 1)
            window_start = None
            window_end = None
            if len(parts) == 2 and parts[1].isdigit():
                try:
                    window_start = datetime.fromtimestamp(int(parts[1]), tz=timezone.utc)
                    window_end = window_start + timedelta(minutes=5)
                except Exception:
                    pass

            if window_end is None:
                end_ts = event.get("endDate")
                if end_ts:
                    try:
                        if end_ts.endswith("Z"):
                            end_ts = end_ts[:-1] + "+00:00"
                        window_end = datetime.fromisoformat(end_ts).astimezone(timezone.utc)
                        window_start = window_end - timedelta(minutes=5)
                    except Exception:
                        pass

            if window_end and (now - timedelta(minutes=1) < window_end < now + timedelta(hours=25)):
                ok_count += 1
            else:
                skip_window += 1
                if ok_count < 3:
                    ws = window_start.isoformat()[:19] if window_start else "?"
                    we = window_end.isoformat()[:19] if window_end else "?"
                    print(f"  ✗ buiten tijdvenster: window_start={ws}, window_end={we}")

    print(f"\n  ✓ bruikbare markets (window_end in 25u): {ok_count}")
    print(f"  ✗ buiten tijdvenster:                   {skip_window}")
    print(f"  ✗ geen clobTokenIds:                    {skip_clob}")

    if ok_count > 0:
        print(f"\n  Eerste 3 events:")
        shown = 0
        for event in matched:
            slug = event.get("slug", "")
            parts = slug.rsplit("-", 1)
            if len(parts) == 2 and parts[1].isdigit():
                ws = datetime.fromtimestamp(int(parts[1]), tz=timezone.utc)
                we = ws + timedelta(minutes=5)
                mins_to = (ws - now).total_seconds() / 60
                if -1 < (we - now).total_seconds() / 60 < 25 * 60:
                    print(f"    slug:         {slug}")
                    print(f"    window_start: {ws.isoformat()[:19]} UTC  ({mins_to:+.0f}min)")
                    shown += 1
                    if shown >= 3:
                        break


async def check_scanner_cache() -> None:
    """Laat zien wat er momenteel in _market_cache van de scanner zit."""
    print(f"\n{SEP}")
    print("  LIVE SCANNER CACHE (wat de bot nu ziet)")
    print(SEP)
    try:
        from src.scanner import _market_cache, refresh_markets
        # Herlaad de cache
        await refresh_markets()
        now = datetime.now(timezone.utc)
        for coin, markets in _market_cache.items():
            active = [m for m in markets
                      if m.get("window_end") and m["window_end"] > now]
            print(f"  {coin}: {len(active)} actieve markten")
            if active:
                first = active[0]
                ws = first.get("window_start")
                mins = (ws - now).total_seconds() / 60 if ws else None
                print(f"    eerstvolgende: {first.get('slug','?')[:50]}")
                print(f"    window_start:  {ws.isoformat()[:19] if ws else '?'} UTC ({mins:+.0f}min)" if mins is not None else "")
    except ImportError as e:
        print(f"  Kan src.scanner niet importeren (run vanuit /opt/poly-baws-bot/backend of met venv): {e}")
    except Exception as e:
        print(f"  Fout: {e}")


async def main():
    print(f"\n{'═'*72}")
    print("  SCANNER DIAGNOSE")
    print(f"{'═'*72}\n")

    # Stap 1: Haal events op van Gamma API
    events = await fetch_events()
    print(f"\n  Totaal events in response: {len(events)}")

    # Stap 2: Check per coin
    for coin, f in FILTERS.items():
        check_coin(coin, f, events)

    # Stap 3: Check scanner cache (alleen als bot-omgeving beschikbaar)
    await check_scanner_cache()

    print(f"\n{'═'*72}\n")


asyncio.run(main())
