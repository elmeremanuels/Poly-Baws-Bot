const BASE = ''

function authHeader(): HeadersInit {
  const creds = sessionStorage.getItem('auth') ?? ''
  return creds ? { Authorization: `Basic ${creds}` } : {}
}

export function setCredentials(user: string, pass: string): void {
  sessionStorage.setItem('auth', btoa(`${user}:${pass}`))
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...authHeader(), ...options.headers },
  })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json() as Promise<T>
}

export const api = {
  status: () => request<BotState>('/api/status'),
  setMode: (mode: string) => request('/api/mode', { method: 'POST', body: JSON.stringify({ mode }) }),
  kill: () => request('/api/kill', { method: 'POST' }),
  resetKill: () => request('/api/kill/reset', { method: 'POST' }),
  triggerHybrid: (marketId: string) =>
    request(`/api/hybrid/trigger/${encodeURIComponent(marketId)}`, { method: 'POST' }),
  trades: (limit = 20) => request<{ trades: Trade[] }>(`/api/trades?limit=${limit}`),
  tradeEvents: (tradeId: string) => request<{ events: Event[] }>(`/api/trades/${tradeId}/events`),
  positions: () => request<{ positions: Trade[] }>('/api/positions'),
  setCoinConfig: (coin: string, config: { enabled?: boolean; max_parallel_positions?: number }) =>
    request(`/api/coins/${coin}/config`, { method: 'POST', body: JSON.stringify(config) }),
  pnlSummary: () => request('/api/pnl/summary'),
}

export interface BotState {
  mode: string
  killed: boolean
  kill_reason: string
  ws_connected: boolean
  daily_pnl: number
  active_trades: Trade[]
  recent_trades: Trade[]
  coins: Record<string, CoinData>
  hybrid_pending: string[]
}

export interface CoinData {
  enabled: boolean
  max_parallel: number
  active_positions: number
  today_pnl: number
  today_trades: number
  next_windows: WindowInfo[]
}

export interface WindowInfo {
  market_id: string
  window_start: string | null
  question: string
}

export interface Trade {
  trade_id: string
  coin: string
  market_id: string
  mode: string
  triggered_by: string
  window_start_ts: string | null
  window_end_ts: string | null
  status: string
  entry_yes_price: number | null
  entry_no_price: number | null
  entry_size: number
  trigger_hit: boolean
  winner_side: string | null
  loser_exit_price: number | null
  winner_exit_price: number | null
  winner_exit_reason: string | null
  fees_paid: number
  gross_pnl: number | null
  net_pnl: number | null
  notes: string
}
