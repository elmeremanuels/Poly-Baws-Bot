import { type Trade } from '../api/client'
import clsx from 'clsx'

interface Props {
  trades: Trade[]
}

export function ActivePositions({ trades }: Props) {
  if (trades.length === 0) {
    return (
      <div className="rounded-xl border border-gray-800 bg-gray-900 p-4">
        <h2 className="font-semibold text-sm uppercase tracking-wide text-gray-400 mb-3">Active Positions</h2>
        <p className="text-gray-600 text-sm">No active positions.</p>
      </div>
    )
  }

  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900 p-4">
      <h2 className="font-semibold text-sm uppercase tracking-wide text-gray-400 mb-3">
        Active Positions ({trades.length})
      </h2>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-gray-500 text-xs border-b border-gray-800">
              <th className="text-left pb-2 pr-3">Coin</th>
              <th className="text-left pb-2 pr-3">Status</th>
              <th className="text-left pb-2 pr-3">Window</th>
              <th className="text-right pb-2 pr-3">YES entry</th>
              <th className="text-right pb-2 pr-3">NO entry</th>
              <th className="text-right pb-2 pr-3">Winner</th>
              <th className="text-right pb-2">Mode</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800/50">
            {trades.map((t) => (
              <PositionRow key={t.trade_id} trade={t} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function PositionRow({ trade: t }: { trade: Trade }) {
  const statusColor: Record<string, string> = {
    pending: 'text-gray-400',
    entry_placed: 'text-amber-400',
    monitoring: 'text-emerald-400',
    exiting: 'text-violet-400',
    closed: 'text-gray-600',
    aborted: 'text-red-400',
    resolved: 'text-blue-400',
  }

  const timeStr = t.window_start_ts
    ? new Date(t.window_start_ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : '—'

  return (
    <tr className="text-gray-300">
      <td className="py-1.5 pr-3 font-semibold">{t.coin}</td>
      <td className={clsx('py-1.5 pr-3 text-xs', statusColor[t.status] ?? 'text-gray-400')}>
        {t.status}
      </td>
      <td className="py-1.5 pr-3 tabular-nums text-gray-400 text-xs">{timeStr}</td>
      <td className="py-1.5 pr-3 text-right tabular-nums text-xs">
        {t.entry_yes_price != null ? `€${t.entry_yes_price.toFixed(2)}` : '—'}
      </td>
      <td className="py-1.5 pr-3 text-right tabular-nums text-xs">
        {t.entry_no_price != null ? `€${t.entry_no_price.toFixed(2)}` : '—'}
      </td>
      <td className="py-1.5 pr-3 text-right text-xs">
        {t.winner_side ? (
          <span className={t.winner_side === 'YES' ? 'text-emerald-400' : 'text-red-400'}>
            {t.winner_side}
          </span>
        ) : (
          '—'
        )}
      </td>
      <td className="py-1.5 text-right text-xs text-gray-500">{t.mode}</td>
    </tr>
  )
}
