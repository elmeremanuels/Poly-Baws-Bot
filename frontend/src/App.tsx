import { useState } from 'react'
import { useBotState } from './hooks/useBotState'
import { api, setCredentials } from './api/client'
import { GlobalControls } from './components/GlobalControls'
import { CoinSection } from './components/CoinSection'
import { HybridTriggerPanel } from './components/HybridTriggerPanel'
import { ActivePositions } from './components/ActivePositions'
import { RecentTrades } from './components/RecentTrades'
import { LogsStream } from './components/LogsStream'

function LoginScreen({ onLogin }: { onLogin: (u: string, p: string) => void }) {
  const [user, setUser] = useState('admin')
  const [pass, setPass] = useState('')
  const [error, setError] = useState('')

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setCredentials(user, pass)
    try {
      await api.status()
      onLogin(user, pass)
    } catch {
      setError('Invalid credentials')
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-950">
      <form onSubmit={submit} className="bg-gray-900 border border-gray-800 rounded-2xl p-8 w-80 flex flex-col gap-4">
        <h1 className="text-xl font-bold text-center text-violet-400">Poly-Baws-Bot</h1>
        <input
          className="bg-gray-950 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 outline-none focus:border-violet-500"
          placeholder="Username"
          value={user}
          onChange={(e) => setUser(e.target.value)}
        />
        <input
          type="password"
          className="bg-gray-950 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 outline-none focus:border-violet-500"
          placeholder="Password"
          value={pass}
          onChange={(e) => setPass(e.target.value)}
        />
        {error && <p className="text-red-400 text-xs text-center">{error}</p>}
        <button
          type="submit"
          className="bg-violet-600 hover:bg-violet-500 text-white rounded-lg py-2 text-sm font-semibold transition-colors"
        >
          Sign in
        </button>
      </form>
    </div>
  )
}

function Dashboard() {
  const { state, connStatus } = useBotState()
  const [, setRefreshKey] = useState(0)

  const refresh = () => setRefreshKey((k) => k + 1)

  if (!state) {
    return (
      <div className="min-h-screen flex items-center justify-center text-gray-500">
        {connStatus === 'connecting' ? 'Connecting…' : 'Disconnected — retrying…'}
      </div>
    )
  }

  const coins = Object.entries(state.coins)

  return (
    <div className="min-h-screen bg-gray-950 p-4 flex flex-col gap-4 max-w-6xl mx-auto">
      {/* Connection banner */}
      {connStatus !== 'connected' && (
        <div className="bg-amber-900/30 border border-amber-800 rounded-lg px-4 py-2 text-amber-400 text-sm text-center">
          WebSocket {connStatus} — data may be stale
        </div>
      )}

      {/* Global controls */}
      <GlobalControls state={state} onRefresh={refresh} />

      {/* Hybrid trigger panel */}
      <HybridTriggerPanel state={state} onRefresh={refresh} />

      {/* Coin grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-3">
        {coins.map(([coin, data]) => (
          <CoinSection key={coin} coin={coin} data={data} onRefresh={refresh} />
        ))}
      </div>

      {/* Positions & trades */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <ActivePositions trades={state.active_trades} />
        <RecentTrades trades={state.recent_trades} />
      </div>

      {/* Logs */}
      <LogsStream />

      <p className="text-center text-gray-700 text-xs pb-2">
        Poly-Baws-Bot · {state.mode} · {new Date().toLocaleTimeString()}
      </p>
    </div>
  )
}

export default function App() {
  const [authed, setAuthed] = useState(() => !!sessionStorage.getItem('auth'))

  if (!authed) {
    return (
      <LoginScreen
        onLogin={(u, p) => {
          setCredentials(u, p)
          setAuthed(true)
        }}
      />
    )
  }

  return <Dashboard />
}
