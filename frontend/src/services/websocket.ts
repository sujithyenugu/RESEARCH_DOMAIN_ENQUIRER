// ============================================================
// WebSocket Manager — token streaming for chat answers
// ============================================================

import type { WsFrame } from '../types';

type MessageHandler = (frame: WsFrame) => void;
type StatusHandler = (status: 'connecting' | 'connected' | 'disconnected' | 'error') => void;

const WS_URL = import.meta.env.VITE_WS_URL ?? 'ws://localhost:8000/ws';
const isMock = import.meta.env.VITE_USE_MOCK_API === 'true';

export class WebSocketManager {
  private ws: WebSocket | null = null;
  private messageHandlers: Set<MessageHandler> = new Set();
  private statusHandlers: Set<StatusHandler> = new Set();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectAttempts = 0;
  private maxReconnects = 5;
  private url: string;

  constructor(url: string = WS_URL) {
    this.url = url;
  }

  connect(): void {
    if (this.ws?.readyState === WebSocket.OPEN) return;

    this.emitStatus('connecting');

    try {
      this.ws = new WebSocket(this.url);

      this.ws.onopen = () => {
        this.reconnectAttempts = 0;
        this.emitStatus('connected');
        console.log('[WS] Connected to', this.url);
      };

      this.ws.onmessage = (event: MessageEvent) => {
        try {
          const frame = JSON.parse(event.data as string) as WsFrame;
          this.messageHandlers.forEach(h => h(frame));
        } catch (err) {
          console.warn('[WS] Could not parse frame:', event.data);
        }
      };

      this.ws.onclose = () => {
        this.emitStatus('disconnected');
        this.scheduleReconnect();
      };

      this.ws.onerror = () => {
        this.emitStatus('error');
      };
    } catch (err) {
      this.emitStatus('error');
      console.error('[WS] Connection failed:', err);
    }
  }

  disconnect(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
  }

  send(data: unknown): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    } else {
      console.warn('[WS] Cannot send — socket not open');
    }
  }

  onMessage(handler: MessageHandler): () => void {
    this.messageHandlers.add(handler);
    return () => this.messageHandlers.delete(handler);
  }

  onStatus(handler: StatusHandler): () => void {
    this.statusHandlers.add(handler);
    return () => this.statusHandlers.delete(handler);
  }

  get isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  private emitStatus(status: Parameters<StatusHandler>[0]): void {
    this.statusHandlers.forEach(h => h(status));
  }

  private scheduleReconnect(): void {
    if (this.reconnectAttempts >= this.maxReconnects) return;
    const delay = Math.min(1000 * 2 ** this.reconnectAttempts, 30_000);
    this.reconnectAttempts++;
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }
}

// Singleton for the app
export const wsManager = new WebSocketManager();

// ----------------------------------------------------------------
// Mock streaming helper — simulates token-by-token streaming
// ----------------------------------------------------------------

import type { QueryResponse } from '../types';

export function mockStream(
  response: QueryResponse,
  onToken: (token: string) => void,
  onComplete: (fullResponse: QueryResponse) => void
): () => void {
  if (!isMock) return () => undefined;

  const words = response.answer.split(' ');
  let idx = 0;
  let cancelled = false;

  const tick = () => {
    if (cancelled || idx >= words.length) {
      if (!cancelled) onComplete(response);
      return;
    }
    const token = (idx === 0 ? '' : ' ') + words[idx++];
    onToken(token);
    const delay = 25 + Math.random() * 35;
    setTimeout(tick, delay);
  };

  setTimeout(tick, 300);

  return () => { cancelled = true; };
}
