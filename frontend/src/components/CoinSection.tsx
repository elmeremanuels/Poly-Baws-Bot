import { useState } from 'react'
import { api, type CoinData } from '../api/client'
import clsx from 'clsx'

const COIN_EMOJI: Record<string, string> = {
  BTC: '₿',
  ETH: 'Ξ',
  SOL: '◎',
  XRP: '✕',
  DOGE: 'Ð',
}

interface Props {
  coin: string
  data: CoinData
  onRefresh: () => void
}

export function CoinSection({ coin, data, onRefresh }: Props) {
  const [loading, setLoading] = useState(false)

  const handleToggle = async () => {
    setLoading(true)
    try {
      await api.setCoinConfig(coin, { enabled: !data.enabled })
      onRefresh()
    } finally {
      setLoading(false)
    }
  }

  const handleMaxChange = async (v: number) => {
    setLoading(true)
    try {
      await api.setCoinConfig(coin, { max_parallel_positions: v })
      onRefresh()
    } finally {
      setLoading(false)
    }
  }

  const pnlColor = data.today_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'

  return (
    <div
      className={clsx(
        'rounded-xl border p-4 flex flex-col gap-3 transition-opacity',
        data.enabled ? 'border-gray-700 bg-gray-900' : 'border-gray-800 bg-gray-950 opacity-60',
      )}
    >
      {/* Header row */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-2xl">{COIN_EMOJI[coin] ?? coin}</span>
          <span className="font-bold text-lg">{coin}</span>
        </div>
        <button
          disabled={loading}
          onClick={handleToggle}
          className={clsx(
            'relative w-10 h-5 rounded-full transition-colors',
            data.enabled ? 'bg-violet-600' : 'bg-gray-700',
          )}
        >
          <span
            className={clsx(
              'absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform',
              data.enabled ? 'translate-x-5' : 'translate-x-0.5',
            )}
          />
        </button>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-3 gap-2 text-center">
        <Stat label="Positions" value={String(data.active_positions)} />
        <Stat label="Trades today" value={String(data.today_trades)} />
        <Stat label="Today P&L" value={`€${data.today_pnl.toFixed(2)}`} valueClass={pnlColor} />
      </div>

      {/* Max positions slider */}
      <div className="flex items-center gap-3 text-sm">
        <span className="text-gray-500 w-28 shrink-0">Max positions</span>
        <input
          type="range"
          min={0}
          max={5}
          value={data.max_parallel}
          disabled={loading || !data.enabled}
          onChange={(e) => handleMaxChange(Number(e.target.value))}
          className="flex-1 accent-violet-500"
        />
        <span className="w-4 text-center tabular-nums text-gray-300">{data.max_parallel}</span>
      </div>

      {/* Upcoming windows */}
      {data.next_windows.length > 0 && (
        <div className="flex flex-col gap-1">
          <span className="text-xs text-gray-500 uppercase tracking-wide">Next windows</span>
          {data.next_windows.map((w) => (
            <div key={w.market_id} className="flex items-center justify-between text-xs bg-gray-800 rounded px-2 py-1">
              <span className="text-gray-400 truncate max-w-[60%]" title={w.question}>
                {w.question || w.market_id}
              </span>
              <span className="text-gray-500 tabular-nums">
                {w.window_start ? formatTime(w.window_start) : '—'}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function Stat({ label, value, valueClass = 'text-gray-100' }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="flex flex-col items-center">
      <span className={clsx('font-semibold tabular-nums', valueClass)}>{value}</span>
      <span className="text-xs text-gray-500">{label}</span>
    </div>
  )
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  } catch {
    return '—'
  }
}
