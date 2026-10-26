import { useEffect, useState } from "react";

const MAX_RECONNECT_ATTEMPTS = 10;
const MAX_BACKOFF_MS = 30000;
const STABLE_CONNECTION_MS = 15000;

function makeWebSocketUrl(analysisId) {
  const configuredWsBase = import.meta.env.VITE_WS_BASE_URL;
  const configuredApiBase = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";
  const parsed = new URL(configuredWsBase || configuredApiBase);
  if (parsed.protocol === "http:") parsed.protocol = "ws:";
  else if (parsed.protocol === "https:") parsed.protocol = "wss:";
  if (!new Set(["ws:", "wss:"]).has(parsed.protocol)) {
    throw new Error("The configured WebSocket base URL must use ws:// or wss://.");
  }
  parsed.pathname = `/ws/v1/analyses/${encodeURIComponent(analysisId)}/`;
  parsed.search = "";
  parsed.hash = "";
  return parsed.toString();
}

/**
 * Maintain an authenticated progress socket with bounded exponential retries.
 * The socket intentionally remains open after analysis completion because a
 * report export can be requested later on the same analysis channel group.
 * REST polling in the console remains the authoritative recovery mechanism.
 *
 * @param {{analysisId:string|null, enabled?:boolean}} options
 * @returns {{connectionState:string, lastEvent:object|null, analysisProgress:object|null, reportProgress:object|null, reconnectAttempt:number}}
 */
export function useAnalysisProgress({ analysisId, enabled = true }) {
  const [connectionState, setConnectionState] = useState("idle");
  const [lastEvent, setLastEvent] = useState(null);
  const [analysisProgress, setAnalysisProgress] = useState(null);
  const [reportProgress, setReportProgress] = useState(null);
  const [reconnectAttempt, setReconnectAttempt] = useState(0);

  useEffect(() => {
    setLastEvent(null);
    setAnalysisProgress(null);
    setReportProgress(null);
    setReconnectAttempt(0);

    if (!enabled || !analysisId) {
      setConnectionState("idle");
      return undefined;
    }

    let disposed = false;
    let socket = null;
    let retryTimer = null;
    let stabilityTimer = null;
    let openedAt = 0;
    let attempt = 0;

    const scheduleReconnect = (permanentFailure = false) => {
      if (disposed) return;
      if (permanentFailure || attempt >= MAX_RECONNECT_ATTEMPTS) {
        setConnectionState("disconnected");
        return;
      }
      attempt += 1;
      setReconnectAttempt(attempt);
      setConnectionState("reconnecting");
      const exponential = Math.min(1000 * (2 ** (attempt - 1)), MAX_BACKOFF_MS);
      const jitter = Math.floor(Math.random() * 350);
      retryTimer = window.setTimeout(connect, exponential + jitter);
    };

    const connect = () => {
      if (disposed) return;
      setConnectionState(attempt > 0 ? "reconnecting" : "connecting");
      let url;
      try {
        url = makeWebSocketUrl(analysisId);
      } catch {
        setConnectionState("unavailable");
        return;
      }

      let candidate;
      try {
        candidate = new WebSocket(url);
        socket = candidate;
      } catch {
        scheduleReconnect();
        return;
      }

      candidate.onopen = () => {
        if (disposed) return;
        openedAt = Date.now();
        setConnectionState("connected");
        if (stabilityTimer !== null) window.clearTimeout(stabilityTimer);
        // Reset the retry budget only when a connection proves stable. A
        // server that repeatedly accepts and quickly drops a socket must not
        // cause an unbounded rapid reconnect loop.
        stabilityTimer = window.setTimeout(() => {
          if (!disposed && candidate.readyState === WebSocket.OPEN) {
            attempt = 0;
            setReconnectAttempt(0);
          }
          stabilityTimer = null;
        }, STABLE_CONNECTION_MS);
      };

      candidate.onmessage = (messageEvent) => {
        if (disposed) return;
        let message;
        try {
          message = JSON.parse(messageEvent.data);
        } catch {
          return;
        }
        if (!message || typeof message.type !== "string") return;
        const event = {
          type: message.type,
          data: message.data && typeof message.data === "object" ? message.data : {},
          receivedAt: Date.now(),
        };
        setLastEvent(event);

        if (["job.progress", "node.completed", "job.completed"].includes(event.type)) {
          setAnalysisProgress(event.data);
        }
        if (["report.progress", "report.completed"].includes(event.type)) {
          setReportProgress(event.data);
        }
        // Do not close the socket on job.completed: report progress can start
        // later, so both workflows share this authenticated connection.
      };

      candidate.onerror = () => {
        if (!disposed) setConnectionState("reconnecting");
        // Browsers dispatch close after most socket errors; only close performs
        // the retry scheduling, preventing duplicate timers.
      };

      candidate.onclose = (closeEvent) => {
        if (disposed) return;
        if (stabilityTimer !== null) {
          window.clearTimeout(stabilityTimer);
          stabilityTimer = null;
        }
        const connectionDuration = openedAt ? Date.now() - openedAt : 0;
        openedAt = 0;
        // Do not hammer the server with retries when session authorization is
        // known to have failed. The user can re-authenticate or select again.
        if ([4401, 4403].includes(closeEvent.code)) {
          setConnectionState("unauthorized");
          return;
        }
        if (closeEvent.code === 4400) {
          setConnectionState("disconnected");
          return;
        }
        if (connectionDuration >= STABLE_CONNECTION_MS) {
          attempt = 0;
          setReconnectAttempt(0);
        }
        scheduleReconnect();
      };
    };

    connect();
    return () => {
      disposed = true;
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      if (stabilityTimer !== null) window.clearTimeout(stabilityTimer);
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        if (
          socket.readyState === WebSocket.OPEN
          || socket.readyState === WebSocket.CONNECTING
        ) {
          socket.close(1000, "View changed");
        }
      }
    };
  }, [analysisId, enabled]);

  return {
    connectionState,
    lastEvent,
    analysisProgress,
    reportProgress,
    reconnectAttempt,
  };
}

export default useAnalysisProgress;
