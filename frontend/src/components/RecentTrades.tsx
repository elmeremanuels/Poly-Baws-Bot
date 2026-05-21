import { useState } from 'react'
import { api, type Trade } from '../api/client'
import clsx from 'clsx'

interface Props {
  trades: Trade[]
}

export function RecentTrades({ trades }: Props) {
  const [expanded, setExpanded] = useState<string | null>(null)
  const [events, setEvents] = useState<unknown[]>([])
  const [eventsLoading, setEventsLoading] = useState(false)

  const handleExpand = async (tradeId: string) => {
    if (expanded === tradeId) {
      setExpanded(null)
      setEvents([])
      return
    }
    setExpanded(tradeId)
    setEventsLoading(true)
    try {
      const res = await api.tradeEvents(tradeId)
      setEvents(res.events)
    } finally {
      setEventsLoading(false)
    }
  }

  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900 p-4">
      <h2 className="font-semibold text-sm uppercase tracking-wide text-gray-400 mb-3">
        Recent Trades ({trades.length})
      </h2>
      {trades.length === 0 ? (
        <p className="text-gray-600 text-sm">No trades yet.</p>
      ) : (
        <div className="flex flex-col gap-1">
          {trades.map((t) => (
            <div key={t.trade_id}>
              <button
                onClick={() => handleExpand(t.trade_id)}
                className="w-full flex items-center justify-between px-3 py-2 rounded-lg hover:bg-gray-800 transition-colors text-sm"
              >
                <div className="flex items-center gap-3">
                  <span className="font-semibold w-10 text-left">{t.coin}</span>
                  <span className={clsx('text-xs px-2 py-0.5 rounded', reasonBadge(t.winner_exit_reason))}>
                    {t.winner_exit_reason ?? t.status}
                  </span>
                  <span className="text-gray-500 text-xs">{t.mode}</span>
                </div>
                <span className={clsx('tabular-nums font-semibold', pnlColor(t.net_pnl))}>
                  {t.net_pnl != null ? `${t.net_pnl >= 0 ? '+' : ''}€${t.net_pnl.toFixed(4)}` : '—'}
                </span>
              </button>

              {expanded === t.trade_id && (
                <div className="mx-2 mb-2 p-3 bg-gray-950 rounded-lg text-xs text-gray-400 space-y-1">
                  <TradeDetail label="Trade ID" value={t.trade_id} mono />
                  <TradeDetail label="Window" value={t.window_start_ts ?? '—'} />
                  <TradeDetail label="Triggered by" value={t.triggered_by} />
                  <TradeDetail label="Winner" value={t.winner_side ?? '—'} />
                  <TradeDetail label="Winner exit" value={t.winner_exit_price != null ? `€${t.winner_exit_price}` : '—'} />
                  <TradeDetail label="Loser exit" value={t.loser_exit_price != null ? `€${t.loser_exit_price}` : '—'} />
                  <TradeDetail label="Fees" value={`€${(t.fees_paid ?? 0).toFixed(4)}`} />
                  <TradeDetail label="Gross P&L" value={t.gross_pnl != null ? `€${t.gross_pnl.toFixed(4)}` : '—'} />
                  {t.notes && <TradeDetail label="Notes" value={t.notes} />}

                  {eventsLoading ? (
                    <p className="text-gray-600 pt-2">Loading events…</p>
                  ) : events.length > 0 ? (
                    <div className="pt-2 border-t border-gray-800">
                      <p className="text-gray-500 mb-1">Events</p>
                      {(events as Record<string, unknown>[]).map((ev, i) => (
                        <div key={i} className="flex gap-2 text-gray-500">
                          <span className="text-gray-600 tabular-nums">{String(ev.ts ?? '')}</span>
                          <span className="text-gray-400">{String(ev.event_type ?? '')}</span>
                          <span className="text-gray-600 truncate">{String(ev.data ?? '')}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function TradeDetail({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex gap-2">
      <span className="text-gray-600 w-28 shrink-0">{label}</span>
      <span className={mono ? 'font-mono text-gray-500 truncate' : 'text-gray-300'}>{value}</span>
    </div>
  )
}

function pnlColor(pnl: number | null): string {
  if (pnl == null) return 'text-gray-500'
  return pnl >= 0 ? 'text-emerald-400' : 'text-red-400'
}

function reasonBadge(reason: string | null): string {
  switch (reason) {
    case 'target_80':
      return 'bg-emerald-900/50 text-emerald-400'
    case 'stop_60':
      return 'bg-red-900/50 text-red-400'
    case 'resolution':
      return 'bg-blue-900/50 text-blue-400'
    case 'abort':
    case 'aborted':
      return 'bg-gray-800 text-gray-500'
    default:
      return 'bg-gray-800 text-gray-500'
  }
}
