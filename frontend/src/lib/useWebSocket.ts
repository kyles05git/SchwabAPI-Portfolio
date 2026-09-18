import { useEffect, useRef, useCallback, useState } from "react";
import type { DashboardData } from "./types";

interface WebSocketConfig {
  url: string;
  onMessage: (data: DashboardData) => void;
  onStatusChange?: (status: ConnectionStatus) => void;
  reconnectInterval?: number;
  maxReconnectAttempts?: number;
}

export type ConnectionStatus = "connecting" | "connected" | "disconnected" | "reconnecting" | "fallback";

/**
 * What the user is actually told about the connection.
 *
 * The WebSocket route is optional, so a failed socket is not by itself a failure:
 * polling is a supported mode, not a degraded one. `reconnecting` and `offline` are
 * reserved for when no transport is currently delivering data.
 */
export type ConnectionState = "live" | "polling" | "reconnecting" | "offline";

/**
 * Derive the user-facing state from the socket status and whether polling is actually
 * delivering data. Once the fallback works, stop presenting it as an active failure.
 */
export function connectionState(
  socket: ConnectionStatus,
  pollingHealthy: boolean
): ConnectionState {
  if (socket === "connected") return "live";
  if (pollingHealthy) return "polling";
  if (socket === "connecting" || socket === "reconnecting") return "reconnecting";
  return "offline";
}

/**
 * WebSocket hook for real-time Schwab streaming data.
 * Falls back to polling if WebSocket connection fails.
 */
export function useWebSocket({
  url,
  onMessage,
  onStatusChange,
  reconnectInterval = 3000,
  maxReconnectAttempts = 5,
}: WebSocketConfig) {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectCount = useRef(0);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");

  const updateStatus = useCallback(
    (s: ConnectionStatus) => {
      setStatus(s);
      onStatusChange?.(s);
    },
    [onStatusChange]
  );

  const connect = useCallback(() => {
    try {
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        reconnectCount.current = 0;
        updateStatus("connected");
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data) as DashboardData;
          onMessage(data);
        } catch {
          // Ignore malformed messages
        }
      };

      ws.onclose = () => {
        if (reconnectCount.current < maxReconnectAttempts) {
          updateStatus("reconnecting");
          reconnectCount.current += 1;
          reconnectTimer.current = setTimeout(connect, reconnectInterval);
        } else {
          updateStatus("fallback");
        }
      };

      ws.onerror = () => {
        ws.close();
      };
    } catch {
      updateStatus("fallback");
    }
  }, [url, onMessage, updateStatus, reconnectInterval, maxReconnectAttempts]);

  const disconnect = useCallback(() => {
    if (reconnectTimer.current) {
      clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    updateStatus("disconnected");
  }, [updateStatus]);

  useEffect(() => {
    connect();
    return () => disconnect();
  }, [connect, disconnect]);

  return { status, disconnect, reconnect: connect };
}