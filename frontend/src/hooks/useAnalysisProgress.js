import { useCallback, useEffect, useState } from "react";

const MAX_RECONNECT_ATTEMPTS = 10;
const MAX_BACKOFF_MS = 30000;
const STABLE_CONNECTION_MS = 15000;

const EMPTY_SESSION = Object.freeze({
  connectionState: "idle",
  lastEvent: null,
  analysisProgress: null,
  reportProgress: null,
  reconnectAttempt: 0,
});

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
 * State is keyed by analysis ID so a newly selected analysis never briefly
 * displays the prior analysis's progress while React runs effect cleanup.
 * The socket stays open after analysis completion because report export can be
 * requested later on the same analysis channel group.
 *
 * @param {{analysisId: string | null, enabled?: boolean}} options
 * @returns {{connectionState: string, lastEvent: object | null, analysisProgress: object | null, reportProgress: object | null, reconnectAttempt: number}}
 */
export function useAnalysisProgress({ analysisId, enabled = true }) {
  const [storedState, setStoredState] = useState({
    analysisId: null,
    ...EMPTY_SESSION,
  });

  const updateState = useCallback((patch) => {
    setStoredState((previous) => {
      const base = previous.analysisId === analysisId
        ? previous
        : {
          analysisId,
          ...EMPTY_SESSION,
          connectionState: enabled && analysisId ? "connecting" : "idle",
        };
      return { ...base, ...patch, analysisId };
    });
  }, [analysisId, enabled]);

  const currentState = storedState.analysisId === analysisId
    ? storedState
    : {
      analysisId,
      ...EMPTY_SESSION,
      connectionState: enabled && analysisId ? "connecting" : "idle",
    };

  useEffect(() => {
    if (!enabled || !analysisId) return undefined;

    let disposed = false;
    let socket = null;
    let retryTimer = null;
    let stabilityTimer = null;
    let openedAt = 0;
    let attempt = 0;

    const scheduleReconnect = (permanentFailure = false) => {
      if (disposed) return;
      if (permanentFailure || attempt >= MAX_RECONNECT_ATTEMPTS) {
        updateState({ connectionState: "disconnected" });
        return;
      }
      attempt += 1;
      updateState({ reconnectAttempt: attempt, connectionState: "reconnecting" });
      const exponential = Math.min(1000 * (2 ** (attempt - 1)), MAX_BACKOFF_MS);
      const jitter = Math.floor(Math.random() * 350);
      retryTimer = window.setTimeout(connect, exponential + jitter);
    };

    const connect = () => {
      if (disposed) return;
      updateState({ connectionState: attempt > 0 ? "reconnecting" : "connecting" });
      let url;
      try {
        url = makeWebSocketUrl(analysisId);
      } catch {
        updateState({ connectionState: "unavailable" });
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
        updateState({ connectionState: "connected" });
        if (stabilityTimer !== null) window.clearTimeout(stabilityTimer);
        // Reset the retry budget only after the connection stays healthy.
        stabilityTimer = window.setTimeout(() => {
          if (!disposed && candidate.readyState === WebSocket.OPEN) {
            attempt = 0;
            updateState({ reconnectAttempt: 0 });
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
        const patch = { lastEvent: event };
        if (["job.progress", "node.completed", "job.completed"].includes(event.type)) {
          patch.analysisProgress = event.data;
        }
        if (["report.progress", "report.completed"].includes(event.type)) {
          patch.reportProgress = event.data;
        }
        updateState(patch);
      };

      candidate.onerror = () => {
        if (!disposed) updateState({ connectionState: "reconnecting" });
        // Browsers usually emit close after an error; close owns retry timing.
      };

      candidate.onclose = (closeEvent) => {
        if (disposed) return;
        if (stabilityTimer !== null) {
          window.clearTimeout(stabilityTimer);
          stabilityTimer = null;
        }
        const connectionDuration = openedAt ? Date.now() - openedAt : 0;
        openedAt = 0;
        if ([4401, 4403].includes(closeEvent.code)) {
          updateState({ connectionState: "unauthorized" });
          return;
        }
        if (closeEvent.code === 4400) {
          updateState({ connectionState: "disconnected" });
          return;
        }
        if (connectionDuration >= STABLE_CONNECTION_MS) {
          attempt = 0;
          updateState({ reconnectAttempt: 0 });
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
        if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
          socket.close(1000, "View changed");
        }
      }
    };
  }, [analysisId, enabled, updateState]);

  return {
    connectionState: !enabled || !analysisId ? "idle" : currentState.connectionState,
    lastEvent: currentState.lastEvent,
    analysisProgress: currentState.analysisProgress,
    reportProgress: currentState.reportProgress,
    reconnectAttempt: currentState.reconnectAttempt,
  };
}

export default useAnalysisProgress;
