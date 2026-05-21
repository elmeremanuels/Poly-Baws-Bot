import { useState } from 'react'
import { api, type BotState } from '../api/client'
import clsx from 'clsx'

const MODES = [
  { value: 'paper_hybrid', label: 'Paper Hybrid' },
  { value: 'paper_auto', label: 'Paper Auto' },
  { value: 'live_hybrid', label: 'Live Hybrid' },
  { value: 'live_auto', label: 'Live Auto' },
]

interface Props {
  state: BotState
  onRefresh: () => void
}

export function GlobalControls({ state, onRefresh }: Props) {
  const [modeLoading, setModeLoading] = useState(false)
  const [killLoading, setKillLoading] = useState(false)

  const handleMode = async (mode: string) => {
    if (mode === state.mode) return
    setModeLoading(true)
    try {
      await api.setMode(mode)
      onRefresh()
    } finally {
      setModeLoading(false)
    }
  }

  const handleKill = async () => {
    setKillLoading(true)
    try {
      if (state.killed) {
        await api.resetKill()
      } else {
        await api.kill()
      }
      onRefresh()
    } finally {
      setKillLoading(false)
    }
  }

  const pnlColor = state.daily_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'

  return (
    <div className="flex items-center gap-4 flex-wrap bg-gray-900 border border-gray-800 rounded-xl px-5 py-3">
      {/* Mode selector */}
      <div className="flex items-center gap-1 bg-gray-950 rounded-lg p-1">
        {MODES.map((m) => (
          <button
            key={m.value}
            disabled={modeLoading || state.killed}
            onClick={() => handleMode(m.value)}
            className={clsx(
              'px-3 py-1.5 rounded-md text-sm font-medium transition-colors',
              state.mode === m.value
                ? 'bg-violet-600 text-white'
                : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800',
            )}
          >
            {m.label}
          </button>
        ))}
      </div>

      {/* Daily P&L */}
      <div className="flex flex-col items-center min-w-[90px]">
        <span className="text-xs text-gray-500 uppercase tracking-wide">Today</span>
        <span className={clsx('text-lg font-semibold tabular-nums', pnlColor)}>
          {state.daily_pnl >= 0 ? '+' : ''}€{state.daily_pnl.toFixed(2)}
        </span>
      </div>

      {/* Connection indicators */}
      <div className="flex gap-3 text-xs ml-auto">
        <StatusDot label="WS" ok={state.ws_connected} />
        <StatusDot label="Bot" ok={!state.killed} />
      </div>

      {/* Kill switch */}
      <button
        onClick={handleKill}
        disabled={killLoading}
        className={clsx(
          'px-4 py-2 rounded-lg font-bold text-sm transition-colors',
          state.killed
            ? 'bg-emerald-600 hover:bg-emerald-500 text-white'
            : 'bg-red-600 hover:bg-red-500 text-white',
        )}
      >
        {state.killed ? 'RESUME' : 'KILL'}
      </button>

      {state.killed && state.kill_reason && (
        <span className="text-xs text-red-400 max-w-[200px] truncate" title={state.kill_reason}>
          {state.kill_reason}
        </span>
      )}
    </div>
  )
}

function StatusDot({ label, ok }: { label: string; ok: boolean }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={clsx('w-2 h-2 rounded-full', ok ? 'bg-emerald-400' : 'bg-red-500')} />
      <span className={ok ? 'text-gray-300' : 'text-red-400'}>{label}</span>
    </span>
  )
}
