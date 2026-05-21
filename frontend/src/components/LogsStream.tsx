import { useEffect, useRef, useState } from 'react'
import clsx from 'clsx'

interface LogEntry {
  ts: string
  level: string
  event: string
  [key: string]: unknown
}

interface Props {
  maxEntries?: number
}

export function LogsStream({ maxEntries = 200 }: Props) {
  const [entries, setEntries] = useState<LogEntry[]>([])
  const [collapsed, setCollapsed] = useState(true)
  const [filter, setFilter] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/ws`)

    ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data as string) as Record<string, unknown>
        if (data.event && data.event !== 'full_state' && data.event !== 'state_update') {
          const entry: LogEntry = {
            ts: new Date().toISOString(),
            level: 'info',
            event: String(data.event),
            ...data,
          }
          setEntries((prev) => {
            const next = [...prev, entry]
            return next.length > maxEntries ? next.slice(-maxEntries) : next
          })
        }
      } catch {
        // ignore
      }
    }

    return () => ws.close()
  }, [maxEntries])

  useEffect(() => {
    if (!collapsed) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [entries, collapsed])

  const visible = filter
    ? entries.filter(
        (e) =>
          e.event.toLowerCase().includes(filter.toLowerCase()) ||
          JSON.stringify(e).toLowerCase().includes(filter.toLowerCase()),
      )
    : entries

  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900">
      <button
        onClick={() => setCollapsed((c) => !c)}
        className="w-full flex items-center justify-between px-4 py-3 text-sm text-gray-400 hover:text-gray-200 transition-colors"
      >
        <span className="font-semibold uppercase tracking-wide text-xs">
          Event Log ({entries.length})
        </span>
        <span>{collapsed ? '▼' : '▲'}</span>
      </button>

      {!collapsed && (
        <div className="border-t border-gray-800">
          <div className="px-4 py-2">
            <input
              type="text"
              placeholder="Filter events…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              className="w-full bg-gray-950 border border-gray-700 rounded px-3 py-1 text-xs text-gray-300 outline-none focus:border-violet-500"
            />
          </div>
          <div className="h-48 overflow-y-auto px-4 pb-3 scrollbar-thin font-mono text-xs space-y-0.5">
            {visible.map((e, i) => (
              <div key={i} className="flex gap-2 text-gray-500 hover:text-gray-300">
                <span className="text-gray-700 tabular-nums shrink-0">
                  {e.ts.slice(11, 19)}
                </span>
                <span className={clsx('shrink-0', levelColor(e.level))}>{e.level}</span>
                <span className="text-gray-400">{e.event}</span>
                <span className="text-gray-700 truncate">
                  {Object.entries(e)
                    .filter(([k]) => !['ts', 'level', 'event'].includes(k))
                    .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
                    .join(' ')}
                </span>
              </div>
            ))}
            <div ref={bottomRef} />
          </div>
        </div>
      )}
    </div>
  )
}

function levelColor(level: string): string {
  switch (level) {
    case 'error':
    case 'critical':
      return 'text-red-500'
    case 'warning':
      return 'text-amber-500'
    default:
      return 'text-blue-500'
  }
}
