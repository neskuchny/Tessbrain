'use client'

import { useEffect, useRef, useState } from 'react'
import { getAccessToken } from './authFetch'

/**
 * useLiveSignals — подписка на /ws/signals для real-time pulse.
 *
 * OSIRIS-урок «движение = жизнь»: даже без новых данных, видимая
 * пульсация после ingest-события («самолёт пролетел») создаёт ощущение
 * живой системы. До этого фронт делал polling 30s — все числа замерзали
 * между запросами.
 *
 * Контракт сервера: backend/api/websocket/signals_ws.py
 *   - on connect: {"type":"connected","payload":{"connection_id":"..."}}
 *   - events:     {"type":"usage_tracked|insight_added|...", "payload":{...}, "ts":"..."}
 *   - keepalive:  {"type":"pulse","payload":{}}  каждые 30s
 *
 * API хука:
 *   - lastSignal: последнее событие (для UI-реакции)
 *   - signalCount: счётчик событий (для key-based перерисовки CSS-анимаций)
 *   - connected: статус соединения
 *
 * Failsafe: если WS недоступен (нет endpoint'а / порт закрыт / сервер
 * перезагружается) — hook молча сидит в disconnected, экран продолжает
 * работать через 30s-polling (см. HomeTab.fetchAll).
 *
 * Reconnect: экспоненциальный backoff (1s → 2s → 4s → 8s → 16s max).
 */

export interface LiveSignal {
  type: string
  payload: Record<string, unknown>
  ts?: string
  tenant_id?: string
}

interface UseLiveSignalsOptions {
  userId?: string | null
  tenantId?: string | null
  /** Подписаться только на эти типы событий (фильтр на стороне сервера). */
  subscribeTo?: string[]
  /** Включить хук. По умолчанию true. Полезно для опт-аут. */
  enabled?: boolean
}

interface UseLiveSignalsResult {
  lastSignal: LiveSignal | null
  signalCount: number
  connected: boolean
}

export function useLiveSignals({
  userId,
  tenantId,
  subscribeTo,
  enabled = true,
}: UseLiveSignalsOptions): UseLiveSignalsResult {
  const [lastSignal, setLastSignal] = useState<LiveSignal | null>(null)
  const [signalCount, setSignalCount] = useState(0)
  const [connected, setConnected] = useState(false)

  const wsRef = useRef<WebSocket | null>(null)
  const reconnectAttemptRef = useRef(0)
  const reconnectTimerRef = useRef<NodeJS.Timeout | null>(null)
  const shouldReconnectRef = useRef(true)

  useEffect(() => {
    if (!enabled) return

    shouldReconnectRef.current = true

    const connect = () => {
      // На стороне Next.js dev: backend по умолчанию на http://localhost:8080
      // или относительный путь, если фронт за reverse-proxy.
      // WS-URL: same-origin в проде, прямой backend в dev. Next.js 14
      // dev-server НЕ умеет проксировать WS upgrade (TypeError
      // 'Cannot read properties of undefined (reading bind)' в логе
      // dev-server). В проде reverse-proxy (nginx/caddy) знает про WS.
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const isDev = typeof window !== 'undefined'
        && /^localhost(:\d+)?$/.test(window.location.host)
        && window.location.port !== '8000'
      const host = isDev
        ? `localhost:${process.env.NEXT_PUBLIC_BACKEND_PORT || '8000'}`
        : window.location.host
      const params = new URLSearchParams()
      if (userId) params.set('user_id', userId)
      if (tenantId) params.set('tenant_id', tenantId)
      // Токен обязателен для tenant-подписки: сервер берёт личность только
      // из проверенного JWT (браузер не умеет ставить заголовки на WS).
      try {
        const tok = getAccessToken()
        if (tok) params.set('token', tok)
      } catch { /* SSR/нет localStorage — соединимся без тенантных событий */ }
      const url = `${protocol}//${host}/ws/signals${params.toString() ? '?' + params : ''}`

      let ws: WebSocket
      try {
        ws = new WebSocket(url)
      } catch (e) {
        // Сервер недоступен — silent fall-back (polling в HomeTab подхватит)
        scheduleReconnect()
        return
      }
      wsRef.current = ws

      ws.onopen = () => {
        setConnected(true)
        reconnectAttemptRef.current = 0
        if (subscribeTo && subscribeTo.length > 0) {
          try {
            ws.send(JSON.stringify({ action: 'subscribe', types: subscribeTo }))
          } catch {
            // ignore
          }
        }
      }

      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data) as LiveSignal
          // pulse-keepalive игнорируем для UI — он только для проверки живости
          if (data.type === 'pulse' || data.type === 'connected' || data.type === 'pong') {
            return
          }
          setLastSignal(data)
          setSignalCount((c) => c + 1)
        } catch {
          // битый payload — игнор
        }
      }

      ws.onerror = () => {
        // не логируем — может быть просто закрытие
      }

      ws.onclose = () => {
        setConnected(false)
        wsRef.current = null
        if (shouldReconnectRef.current) {
          scheduleReconnect()
        }
      }
    }

    const scheduleReconnect = () => {
      const attempt = Math.min(reconnectAttemptRef.current, 4)
      reconnectAttemptRef.current += 1
      // 1s → 2s → 4s → 8s → 16s max
      const delay = Math.min(1000 * 2 ** attempt, 16000)
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current)
      }
      reconnectTimerRef.current = setTimeout(connect, delay)
    }

    connect()

    return () => {
      shouldReconnectRef.current = false
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
      if (wsRef.current) {
        try {
          wsRef.current.close()
        } catch {
          // ignore
        }
        wsRef.current = null
      }
    }
  }, [userId, tenantId, enabled, subscribeTo?.join(',')])

  return { lastSignal, signalCount, connected }
}
