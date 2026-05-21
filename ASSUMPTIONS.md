# Implementation Assumptions

## Polymarket API
- **Market discovery**: Uses Gamma API (`gamma-api.polymarket.com/markets`) with tag-based filtering. The Gamma API is Polymarket's public market metadata API. Market slugs/questions are filtered by coin-specific strings from `config.yaml`. If Polymarket changes their API structure, `scanner.py` normalization logic needs updating.
- **CLOB WebSocket URL**: Uses `wss://ws-subscriptions-clob.polymarket.com/v2/ws` (v2 endpoint). Subscribe format sends `asset_ids` array. If the WS protocol changes, update `ws_client.py`.
- **Token IDs**: YES/NO token IDs are taken from `tokens[0]` and `tokens[1]` of the Gamma market response. If ordering is different for some markets, add explicit side-detection via token metadata.
- **Fee rate**: Hardcoded at 2% in `paper_trader.py`. Verify current fee schedule at https://docs.polymarket.com before going live.
- **Order signing**: Uses `py-clob-client` with `signature_type=1` (proxy wallet) when `POLYMARKET_PROXY_ADDRESS` is set, `signature_type=0` (EOA) otherwise.
- **USDC balance unit**: Assumes balance returned from `get_balance_allowance` is in micro-USDC (6 decimals). If returned in USDC directly, remove the `/ 1e6`.

## Strategy
- **Entry price deviation**: Orders placed at `entry_price_target ± entry_price_max_deviation`. In paper mode, fills accepted if book has depth at `limit_price + 0.5¢ slippage`.
- **OCO exit**: True one-cancels-other is not available via CLOB API. Implemented as price monitoring loop that places market order when threshold is crossed. In production, this may result in slightly worse fills than a native OCO.
- **Resolution outcome**: When window expires without trigger or exit, trade is logged as `resolution`. Actual resolution payout ($1 winner, $0 loser) is settled by Polymarket — the bot does not explicitly claim it.

## Dashboard
- **Single-user auth**: HTTP Basic Auth. Credentials checked in Python against env vars. Not suitable for public internet without TLS.
- **State broadcast**: All connected WebSocket clients receive the same state snapshot. No per-user state.
- **Coin config at runtime**: Changes to `enabled` and `max_parallel_positions` via dashboard are applied in-memory to `CONFIG` dict and persisted to `dashboard_state` SQLite table. They do NOT write back to `config.yaml`.

## Deployment
- **Process manager**: systemd service file provided. PM2 is equally valid; adapt accordingly.
- **Frontend serving**: FastAPI serves the built React `dist/` as static files. In dev, use `vite dev` proxy mode (see `vite.config.ts`).
- **TLS**: Caddy is suggested. Replace `your-domain.com` in `Caddyfile` with actual domain.

## Paper Mode Realism
- Slippage buffer: +0.5¢ per share on all paper fills, making paper results conservatively pessimistic.
- Paper fills only execute if the required depth exists in the live orderbook at the time of the simulated order. No "guaranteed fill" assumption.
- Multi-level book walking is implemented for both limit and market orders.
