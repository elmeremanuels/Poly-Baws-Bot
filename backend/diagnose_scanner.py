#!/usr/bin/env python3
"""
Run on the server to inspect what the Gamma API actually returns.

Usage:
  /opt/poly-baws-bot/venv/bin/python /opt/poly-baws-bot/backend/diagnose_scanner.py
"""
import json
import httpx

GAMMA = "https://gamma-api.polymarket.com"

def show(label: str, resp: httpx.Response) -> None:
    data = resp.json()
    items = data if isinstance(data, list) else (
        data.get("markets") or data.get("events") or []
    )
    print(f"\n{'='*60}")
    print(f"{label}  status={resp.status_code}  items={len(items)}")
    print(f"{'='*60}")
    for item in items[:4]:
        child_count = len(item.get("markets") or [])
        tags = [
            (t.get("label") or t.get("slug") or t.get("id") or str(t))
            for t in (item.get("tags") or [])
        ]
        print(json.dumps({
            "id":        item.get("id"),
            "slug":      (item.get("slug") or "")[:70],
            "question":  (item.get("question") or item.get("title") or "")[:70],
            "active":    item.get("active"),
            "closed":    item.get("closed"),
            "startDate": item.get("startDate"),
            "endDate":   item.get("endDate"),
            "tags":      tags[:5],
            "child_markets": child_count,
        }, indent=2))

with httpx.Client(timeout=15) as c:
    # ── Events endpoint ──────────────────────────────────────────────────────
    show("GET /events  (no filter, latest 5)",
         c.get(f"{GAMMA}/events", params={"limit": 5, "order": "startDate", "ascending": "false"}))

    show("GET /events  tag=crypto",
         c.get(f"{GAMMA}/events", params={"tag": "crypto", "limit": 5}))

    show("GET /events  tag=BTC",
         c.get(f"{GAMMA}/events", params={"tag": "BTC", "limit": 5}))

    show("GET /events  active=true  closed=false  tag=BTC",
         c.get(f"{GAMMA}/events", params={"tag": "BTC", "active": "true", "closed": "false", "limit": 5}))

    # ── Markets endpoint ─────────────────────────────────────────────────────
    show("GET /markets  tag=BTC  active=true  closed=false",
         c.get(f"{GAMMA}/markets", params={"tag": "BTC", "active": "true", "closed": "false", "limit": 5}))

    show("GET /markets  tag=BTC  (no active filter)",
         c.get(f"{GAMMA}/markets", params={"tag": "BTC", "limit": 5, "order": "startDate", "ascending": "false"}))

    show("GET /markets  tag=crypto  (no active filter, latest 5)",
         c.get(f"{GAMMA}/markets", params={"tag": "crypto", "limit": 5, "order": "startDate", "ascending": "false"}))

    # ── Slug search ──────────────────────────────────────────────────────────
    for slug_hint in ["5m", "5-min", "bitcoin", "btc"]:
        show(f"GET /markets  slug_contains={slug_hint!r}  latest 3",
             c.get(f"{GAMMA}/markets", params={"slug_contains": slug_hint, "limit": 3,
                                               "order": "startDate", "ascending": "false"}))
