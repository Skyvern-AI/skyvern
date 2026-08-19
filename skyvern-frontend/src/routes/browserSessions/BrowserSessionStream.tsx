import { useEffect, useRef, useState } from "react";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { newWssBaseUrl, getCredentialParam } from "@/util/env";
import { useCdpInput } from "@/routes/streaming/useCdpInput";
import { InteractiveStreamView } from "@/routes/streaming/InteractiveStreamView";
import {
  markCommit,
  markLoad,
  markMessage,
  startDebugReport,
} from "@/routes/streaming/streamStats";
import {
  StreamStatusPanel,
  type StreamDiagnostic,
} from "@/routes/streaming/StreamDiagnostics";
import {
  STREAM_MAX_RECONNECT_ATTEMPTS,
  STREAM_RECONNECT_DELAY_MS,
  diagnosticForStatus,
  isTerminalStreamStatus,
  shouldReconnectStream,
} from "./BrowserSessionStream.utils";
import { useSettingsStore } from "@/store/SettingsStore";

type StreamMessage = {
  browser_session_id?: string;
  status: string;
  screenshot?: string;
  format?: string;
  viewport_width?: number;
  viewport_height?: number;
  url?: string;
};

const STARTING_DIAGNOSTIC: StreamDiagnostic = {
  title: "Waking up your local browser",
  detail: "Opening the stream and waiting for the first frame...",
  pending: true,
};

function diagnosticForReconnectExhausted(): StreamDiagnostic {
  return {
    title: "Stream connection dropped",
    detail: "The browser stream disconnected and could not reconnect.",
    hint: "Refresh the editor or create a new browser session.",
  };
}

function diagnosticForClose(event: CloseEvent): StreamDiagnostic {
  if (event.code === 4001 || event.reason === "use-vnc-streaming") {
    return {
      title: "Backend wants to use VNC streaming",
      detail:
        "The UI tried local browser streaming, but the backend asked for VNC instead.",
      hint: "Check BROWSER_STREAMING_MODE on the backend and the runtime config response.",
    };
  }
  if (event.code === 1006) {
    return {
      title: "The connection slipped away",
      detail: "The browser stream WebSocket closed before sending a frame.",
      hint: "Check that the API server is running and reachable from the UI.",
    };
  }
  return {
    title: "The stream packed up and left",
    detail: `WebSocket closed with code ${event.code}${event.reason ? ` (${event.reason})` : ""}.`,
  };
}

interface Props {
  browserSessionId: string;
  interactive?: boolean;
  showControlButtons?: boolean;
  centered?: boolean;
  onReadyChange?: (isReady: boolean, browserSessionId: string | null) => void;
  onUrlChange?: (url: string) => void;
  onActivity?: () => void;
  // Bypasses a stale RFB selection after the authenticated RFB socket has failed.
  forceCdp?: boolean;
  // Opt-in: turns the read-only URL bar into a navigable input. Only the
  // hosted-browser-session live view passes this today (SKY-13683) -- the
  // workflow studio/editor callers of this component leave it unset and keep
  // today's read-only display.
  enableUrlInput?: boolean;
  // Passing this hands the window frame to the caller (see InteractiveStreamView).
  onFrameWidthChange?: (width: number | null) => void;
}

function BrowserSessionStream({
  browserSessionId,
  interactive = false,
  showControlButtons = false,
  centered = false,
  onReadyChange,
  onUrlChange,
  onActivity,
  forceCdp = false,
  enableUrlInput = false,
  onFrameWidthChange,
}: Props) {
  const [streamImgSrc, setStreamImgSrc] = useState<string>("");
  const [streamImgToken, setStreamImgToken] = useState<number>(0);
  const [streamFormat, setStreamFormat] = useState<string>("png");
  const [viewportWidth, setViewportWidth] = useState(1280);
  const [viewportHeight, setViewportHeight] = useState(720);
  const [currentUrl, setCurrentUrl] = useState("");
  const [diagnostic, setDiagnostic] =
    useState<StreamDiagnostic>(STARTING_DIAGNOSTIC);
  const credentialGetter = useCredentialGetter();
  const settingsStore = useSettingsStore();

  const socketRef = useRef<WebSocket | null>(null);
  const onActivityRef = useRef(onActivity);
  const hasFrameRef = useRef(false);
  const pendingFrameRef = useRef<{
    token: number;
    screenshot: string;
    message: StreamMessage;
  } | null>(null);
  const rafRef = useRef<number | null>(null);
  const lastCommittedTokenRef = useRef<number>(0);
  const reconnectAttemptsRef = useRef(0);
  const terminalStatusSeenRef = useRef(false);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // The CDP input socket must be wired whenever the stream can be controlled,
  // whether by default interaction or via the take-control button.
  const controllable = interactive || showControlButtons;

  const inputWsUrl = controllable
    ? `${newWssBaseUrl}/stream/cdp_input/browser_session/${browserSessionId}`
    : null;

  const {
    userIsControlling,
    setUserIsControlling,
    inputReady,
    containerRef,
    handlers,
    navigate,
    historyNavigate,
    navigateError,
  } = useCdpInput({
    inputWsUrl,
    interactive: controllable,
    viewportWidth,
    viewportHeight,
  });

  useEffect(() => {
    onActivityRef.current = onActivity;
  }, [onActivity]);

  useEffect(() => startDebugReport(), []);

  // Once control can't be offered (input socket torn down), forget any prior
  // grab so re-enabling doesn't silently restore control without a new click.
  useEffect(() => {
    if (!controllable) {
      setUserIsControlling(false);
    }
  }, [controllable, setUserIsControlling]);

  useEffect(() => {
    let cancelled = false;
    setStreamImgSrc("");
    setStreamFormat("png");
    setViewportWidth(1280);
    setViewportHeight(720);
    setCurrentUrl("");
    setDiagnostic(STARTING_DIAGNOSTIC);
    hasFrameRef.current = false;
    reconnectAttemptsRef.current = 0;
    terminalStatusSeenRef.current = false;

    const clearReconnectTimer = () => {
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };

    const clearPendingFrame = () => {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      pendingFrameRef.current = null;
    };

    async function connect() {
      const credentialParam = await getCredentialParam(credentialGetter);
      if (cancelled) {
        return;
      }

      socketRef.current?.close();
      const socket = new WebSocket(
        `${newWssBaseUrl}/stream/browser_sessions/${browserSessionId}?${credentialParam}${forceCdp ? "&force_cdp=true" : ""}`,
      );
      socketRef.current = socket;

      const isCurrentSocket = () => !cancelled && socketRef.current === socket;

      socket.addEventListener("open", () => {
        if (!isCurrentSocket()) {
          return;
        }
        setDiagnostic({
          title: "Hooked up to the stream",
          detail: "Just waiting for the backend to hand us a browser.",
          pending: true,
        });
      });

      socket.addEventListener("message", (event) => {
        if (!isCurrentSocket()) {
          return;
        }
        try {
          const message: StreamMessage = JSON.parse(event.data);
          if (
            message.browser_session_id !== undefined &&
            message.browser_session_id !== browserSessionId
          ) {
            return;
          }
          const hasActivity =
            Boolean(message.screenshot) || message.url !== undefined;
          // Presentation metadata is committed only with its pixels. Applying it
          // on receipt would pair the new viewport (which maps click coordinates)
          // with the previous screenshot.
          const applyMetadata = (m: StreamMessage) => {
            if (m.format) {
              setStreamFormat((prev) => (prev === m.format ? prev : m.format!));
            }
            if (m.viewport_width) {
              setViewportWidth((prev) =>
                prev === m.viewport_width ? prev : m.viewport_width!,
              );
            }
            if (m.viewport_height) {
              setViewportHeight((prev) =>
                prev === m.viewport_height ? prev : m.viewport_height!,
              );
            }
            if (m.url !== undefined) {
              setCurrentUrl((prev) => (prev === m.url ? prev : m.url!));
            }
          };
          if (message.screenshot) {
            hasFrameRef.current = true;
            reconnectAttemptsRef.current = 0;
            const token = markMessage();
            pendingFrameRef.current = {
              token,
              screenshot: message.screenshot,
              message,
            };
            if (rafRef.current === null) {
              rafRef.current = requestAnimationFrame(() => {
                rafRef.current = null;
                const pending = pendingFrameRef.current;
                if (!pending || !isCurrentSocket()) return;
                pendingFrameRef.current = null;
                lastCommittedTokenRef.current = pending.token;
                setStreamImgSrc(pending.screenshot);
                setStreamImgToken(pending.token);
                applyMetadata(pending.message);
                markCommit(pending.token);
              });
            }
          }
          if (hasActivity) {
            onActivityRef.current?.();
          }
          const isTerminal = isTerminalStreamStatus(message.status);
          if (message.status && (isTerminal || !message.screenshot)) {
            setDiagnostic(diagnosticForStatus(message.status));
          }
          if (isTerminal) {
            terminalStatusSeenRef.current = true;
            // Drop the last frame: keeping it leaves a dead, still-interactive
            // screenshot covering the terminal status panel.
            clearPendingFrame();
            setStreamImgSrc("");
            socket.close();
          }
        } catch (e) {
          console.error("Failed to parse message", e);
          setDiagnostic({
            title: "The stream said something funny",
            detail: "The browser sent a message the UI couldn't parse.",
          });
        }
      });

      socket.addEventListener("error", () => {
        if (!isCurrentSocket()) {
          return;
        }
        setDiagnostic({
          title: "The stream hit a snag",
          detail: "The connection ran into a network or server error.",
        });
      });

      socket.addEventListener("close", (event) => {
        if (socketRef.current !== socket) {
          return;
        }
        clearPendingFrame();
        socketRef.current = null;

        if (
          !cancelled &&
          !hasFrameRef.current &&
          !terminalStatusSeenRef.current
        ) {
          setDiagnostic(diagnosticForClose(event));
        }
        const canReconnect =
          !cancelled &&
          shouldReconnectStream({
            closeCode: event.code,
            closeReason: event.reason,
            terminalStatusSeen: terminalStatusSeenRef.current,
            reconnectAttempts: reconnectAttemptsRef.current,
          });

        if (canReconnect) {
          reconnectAttemptsRef.current += 1;
          if (!hasFrameRef.current) {
            setDiagnostic({
              ...diagnosticForClose(event),
              hint: `Reconnecting in ${STREAM_RECONNECT_DELAY_MS / 1000}s (${reconnectAttemptsRef.current}/${STREAM_MAX_RECONNECT_ATTEMPTS}).`,
            });
          }
          clearReconnectTimer();
          reconnectTimerRef.current = setTimeout(() => {
            reconnectTimerRef.current = null;
            void connect();
          }, STREAM_RECONNECT_DELAY_MS);
        } else if (
          !cancelled &&
          !terminalStatusSeenRef.current &&
          hasFrameRef.current &&
          reconnectAttemptsRef.current >= STREAM_MAX_RECONNECT_ATTEMPTS
        ) {
          hasFrameRef.current = false;
          setStreamImgSrc("");
          setDiagnostic(diagnosticForReconnectExhausted());
        }
      });
    }
    void connect();

    return () => {
      cancelled = true;
      clearReconnectTimer();
      clearPendingFrame();
      const socket = socketRef.current;
      if (socket) {
        socketRef.current = null;
        socket.close();
      }
    };
  }, [credentialGetter, browserSessionId, forceCdp]);

  const isReady = streamImgSrc.length > 0;

  useEffect(() => {
    onUrlChange?.(currentUrl);
  }, [currentUrl, onUrlChange]);

  useEffect(() => {
    // browserSessionId intentionally not a dep: re-firing on prop change
    // before isReady resets would spuriously report (true, newSessionId).
    onReadyChange?.(isReady, isReady ? browserSessionId : null);
    // Zustand store setters are stable; omit browserSessionId from deps on purpose.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isReady, onReadyChange]);

  useEffect(() => {
    return () => {
      onReadyChange?.(false, null);
    };
  }, [onReadyChange]);

  useEffect(() => {
    settingsStore.setIsUsingABrowser(isReady);
    settingsStore.setBrowserSessionId(isReady ? browserSessionId : null);
    // Zustand store setters are stable; only sync when stream readiness changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isReady, browserSessionId]);

  if (isReady) {
    return (
      <InteractiveStreamView
        streamImgSrc={streamImgSrc}
        streamFormat={streamFormat}
        interactive={controllable}
        userIsControlling={userIsControlling}
        setUserIsControlling={setUserIsControlling}
        inputReady={inputReady}
        containerRef={containerRef}
        showControlButtons={showControlButtons}
        handlers={handlers}
        currentUrl={currentUrl}
        centered={centered}
        onNavigate={enableUrlInput ? navigate : undefined}
        navigateError={enableUrlInput ? navigateError : undefined}
        onHistoryNavigate={enableUrlInput ? historyNavigate : undefined}
        onFrameWidthChange={onFrameWidthChange}
        frameToken={streamImgToken}
        onFrameLoad={markLoad}
      />
    );
  }

  return <StreamStatusPanel diagnostic={diagnostic} />;
}

export { BrowserSessionStream };
