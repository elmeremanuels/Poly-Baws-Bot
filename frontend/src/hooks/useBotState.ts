import { useEffect, useRef, useState, useCallback } from 'react'
import { type BotState } from '../api/client'

type ConnectionStatus = 'connecting' | 'connected' | 'disconnected'

export function useBotState() {
  const [state, setState] = useState<BotState | null>(null)
  const [connStatus, setConnStatus] = useState<ConnectionStatus>('connecting')
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const backoff = useRef(1000)

  const connect = useCallback(() => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/ws`)
    wsRef.current = ws

    ws.onopen = () => {
      setConnStatus('connected')
      backoff.current = 1000
    }

    ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data as string) as Partial<BotState> & { event?: string }
        if (data.event === 'full_state' || data.event === 'state_update') {
          const { event: _e, ...rest } = data
          setState(rest as BotState)
        }
      } catch {
        // ignore
      }
    }

    ws.onclose = () => {
      setConnStatus('disconnected')
      reconnectTimer.current = setTimeout(() => {
        backoff.current = Math.min(backoff.current * 2, 30000)
        setConnStatus('connecting')
        connect()
      }, backoff.current)
    }

    ws.onerror = () => {
      ws.close()
    }
  }, [])

  useEffect(() => {
    connect()
    return () => {
      wsRef.current?.close()
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
    }
  }, [connect])

  const sendPing = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send('ping')
    }
  }, [])

  useEffect(() => {
    const interval = setInterval(sendPing, 20000)
    return () => clearInterval(interval)
  }, [sendPing])

  return { state, connStatus }
}
