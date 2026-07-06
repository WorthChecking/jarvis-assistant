/**
 * WebSocket 客户端 — 连接 Python 后端
 * 心跳保活 + 指数退避重连 + 消息分发
 */

import { UIState, VolumeRMSData, StateChangeData, ActionStartData } from '../types'

type MessageHandler = (data: unknown) => void

export class WSClient {
  private ws: WebSocket | null = null
  private url: string
  private handlers: Map<string, MessageHandler[]> = new Map()
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private reconnectDelay: number = 1000
  private maxReconnectDelay: number = 30000
  private missedPongs: number = 0
  private maxMissedPongs: number = 3
  private running: boolean = false

  constructor(url?: string) {
    this.url = url || import.meta.env.VITE_WS_URL || 'ws://127.0.0.1:8765/ws'
  }

  on(type: string, handler: MessageHandler): void {
    if (!this.handlers.has(type)) {
      this.handlers.set(type, [])
    }
    this.handlers.get(type)!.push(handler)
  }

  off(type: string, handler: MessageHandler): void {
    const arr = this.handlers.get(type)
    if (arr) {
      const idx = arr.indexOf(handler)
      if (idx >= 0) arr.splice(idx, 1)
    }
  }

  send(type: string, data: unknown): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type, data }))
    }
  }

  start(): void {
    this.running = true
    this.connect()
  }

  stop(): void {
    this.running = false
    this.clearTimers()
    if (this.ws) {
      this.send('close', { reason: 'user_exit' })
      this.ws.close()
      this.ws = null
    }
  }

  private connect(): void {
    if (!this.running) return
    try {
      this.ws = new WebSocket(this.url)
    } catch {
      this.scheduleReconnect()
      return
    }

    this.ws.onopen = () => {
      this.reconnectDelay = 1000
      this.missedPongs = 0
      this.startHeartbeat()
    }

    this.ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data)
        this.dispatch(msg.type, msg.data)
      } catch { /* 忽略非 JSON 消息 */ }
    }

    this.ws.onclose = () => {
      this.clearTimers()
      this.scheduleReconnect()
    }

    this.ws.onerror = () => {
      this.ws?.close()
    }
  }

  private dispatch(type: string, data: unknown): void {
    if (type === 'pong') {
      this.missedPongs = 0
    }
    const arr = this.handlers.get(type)
    if (arr) {
      for (const fn of arr) fn(data)
    }
  }

  private startHeartbeat(): void {
    this.heartbeatTimer = setInterval(() => {
      this.missedPongs++
      if (this.missedPongs > this.maxMissedPongs) {
        this.ws?.close()
        return
      }
      this.send('ping', { timestamp: Date.now() })
    }, 5000)
  }

  private scheduleReconnect(): void {
    if (!this.running) return
    this.reconnectTimer = setTimeout(() => {
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxReconnectDelay)
      this.connect()
    }, this.reconnectDelay)
  }

  private clearTimers(): void {
    if (this.heartbeatTimer) { clearInterval(this.heartbeatTimer); this.heartbeatTimer = null }
    if (this.reconnectTimer) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null }
  }
}
