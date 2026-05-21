import { useState } from 'react'
import { api, type BotState } from '../api/client'
import clsx from 'clsx'

interface Props {
  state: BotState
  onRefresh: () => void
}

export function HybridTriggerPanel({ state, onRefresh }: Props) {
  const isHybrid = state.mode.includes('hybrid')
  const pending = state.hybrid_pending ?? []

  if (!isHybrid) return null

  return (
    <div className="rounded-xl border border-amber-900/50 bg-amber-950/20 p-4">
      <div className="flex items-center gap-2 mb-3">
        <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
        <h2 className="font-semibold text-amber-300 text-sm uppercase tracking-wide">
          Hybrid Trigger Panel
        </h2>
      </div>

      {pending.length === 0 ? (
        <p className="text-gray-500 text-sm">No windows ready for manual trigger.</p>
      ) : (
        <div className="flex flex-col gap-2">
          {pending.map((marketId) => (
            <TriggerRow key={marketId} marketId={marketId} state={state} onRefresh={onRefresh} />
          ))}
        </div>
      )}
    </div>
  )
}

function TriggerRow({
  marketId,
  state,
  onRefresh,
}: {
  marketId: string
  state: BotState
  onRefresh: () => void
}) {
  const [triggered, setTriggered] = useState(false)
  const [loading, setLoading] = useState(false)

  // Find window info from coins
  let windowStart: string | null = null
  let coin = ''
  for (const [c, data] of Object.entries(state.coins)) {
    const w = data.next_windows.find((w) => w.market_id === marketId)
    if (w) {
      windowStart = w.window_start
      coin = c
      break
    }
  }

  const handleTrigger = async () => {
    setLoading(true)
    try {
      await api.triggerHybrid(marketId)
      setTriggered(true)
      onRefresh()
    } finally {
      setLoading(false)
    }
  }

  const timeStr = windowStart
    ? new Date(windowStart).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    : '—'

  return (
    <div className="flex items-center justify-between bg-gray-900 border border-gray-700 rounded-lg px-4 py-2">
      <div className="flex flex-col gap-0.5">
        <span className="text-sm font-medium text-gray-200">
          {coin || '?'} · {timeStr}
        </span>
        <span className="text-xs text-gray-500 font-mono truncate max-w-[220px]">{marketId}</span>
      </div>
      <button
        disabled={triggered || loading || state.killed}
        onClick={handleTrigger}
        className={clsx(
          'px-4 py-1.5 rounded-lg text-sm font-semibold transition-colors',
          triggered
            ? 'bg-gray-700 text-gray-400 cursor-default'
            : 'bg-amber-500 hover:bg-amber-400 text-gray-950',
        )}
      >
        {triggered ? 'Triggered' : 'Trigger Entry'}
      </button>
    </div>
  )
}
