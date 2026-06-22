import { useEffect, useRef, useState } from "react";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { newWssBaseUrl, getCredentialParam } from "@/util/env";
import { useCdpInput } from "@/routes/streaming/useCdpInput";
import { InteractiveStreamView } from "@/routes/streaming/InteractiveStreamView";
import {
  StreamStatusPanel,
  type StreamDiagnostic,
} from "@/routes/streaming/StreamDiagnostics";
import {
  STREAM_MAX_RECONNECT_ATTEMPTS,
  STREAM_RECONNECT_DELAY_MS,
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

function diagnosticForStatus(status: string): StreamDiagnostic {
  switch (status) {
    case "not_found":
      return {
        title: "We've misplaced this browser session",
        detail: "The backend can't find it for your org.",
        hint: "Refresh the page or spin up a fresh browser session.",
      };
    case "timeout":
      return {
        title: "The browser's gone strangely quiet",
        detail:
          "The stream connected, but no active page showed up to screencast.",
        hint: "Check backend logs for browser launch errors and verify BROWSER_STREAMING_MODE=cdp.",
      };
    case "completed":
    case "failed":
      return {
        title: "This browser session has wandered off",
        detail: `It's no longer live — status: ${status}.`,
      };
    default:
      return {
        title: "Waiting for browser frames",
        detail: `The stream is connected and the session status is ${status}.`,
        pending: true,
      };
  }
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
  onReadyChange?: (isReady: boolean, browserSessionId: string | null) => void;
}

function BrowserSessionStream({
  browserSessionId,
  interactive = false,
  showControlButtons = false,
  onReadyChange,
}: Props) {
  const [streamImgSrc, setStreamImgSrc] = useState<string>("");
  const [streamFormat, setStreamFormat] = useState<string>("png");
  const [viewportWidth, setViewportWidth] = useState(1280);
  const [viewportHeight, setViewportHeight] = useState(720);
  const [currentUrl, setCurrentUrl] = useState("");
  const [diagnostic, setDiagnostic] =
    useState<StreamDiagnostic>(STARTING_DIAGNOSTIC);
  const credentialGetter = useCredentialGetter();
  const settingsStore = useSettingsStore();

  const socketRef = useRef<WebSocket | null>(null);
  const hasFrameRef = useRef(false);
  const reconnectAttemptsRef = useRef(0);
  const terminalStatusSeenRef = useRef(false);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const inputWsUrl = interactive
    ? `${newWssBaseUrl}/stream/cdp_input/browser_session/${browserSessionId}`
    : null;

  const {
    userIsControlling,
    setUserIsControlling,
    inputReady,
    containerRef,
    handlers,
  } = useCdpInput({
    inputWsUrl,
    interactive,
    viewportWidth,
    viewportHeight,
  });

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

    async function connect() {
      const credentialParam = await getCredentialParam(credentialGetter);
      if (cancelled) {
        return;
      }

      if (socketRef.current) {
        socketRef.current.close();
      }
      socketRef.current = new WebSocket(
        `${newWssBaseUrl}/stream/browser_sessions/${browserSessionId}?${credentialParam}`,
      );

      socketRef.current.addEventListener("open", () => {
        setDiagnostic({
          title: "Hooked up to the stream",
          detail: "Just waiting for the backend to hand us a browser.",
          pending: true,
        });
      });

      socketRef.current.addEventListener("message", (event) => {
        try {
          const message: StreamMessage = JSON.parse(event.data);
          if (message.screenshot) {
            hasFrameRef.current = true;
            reconnectAttemptsRef.current = 0;
            setStreamImgSrc(message.screenshot);
          }
          if (message.format) {
            setStreamFormat(message.format);
          }
          if (message.viewport_width) {
            setViewportWidth(message.viewport_width);
          }
          if (message.viewport_height) {
            setViewportHeight(message.viewport_height);
          }
          if (message.url !== undefined) {
            setCurrentUrl(message.url);
          }
          if (!message.screenshot && message.status) {
            setDiagnostic(diagnosticForStatus(message.status));
          }
          if (isTerminalStreamStatus(message.status)) {
            terminalStatusSeenRef.current = true;
            socketRef.current?.close();
          }
        } catch (e) {
          console.error("Failed to parse message", e);
          setDiagnostic({
            title: "The stream said something funny",
            detail: "The browser sent a message the UI couldn't parse.",
          });
        }
      });

      socketRef.current.addEventListener("error", () => {
        setDiagnostic({
          title: "The stream hit a snag",
          detail: "The connection ran into a network or server error.",
        });
      });

      socketRef.current.addEventListener("close", (event) => {
        if (
          !cancelled &&
          !hasFrameRef.current &&
          !terminalStatusSeenRef.current
        ) {
          setDiagnostic(diagnosticForClose(event));
        }
        socketRef.current = null;

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
      if (socketRef.current) {
        socketRef.current.close();
        socketRef.current = null;
      }
    };
  }, [credentialGetter, browserSessionId]);

  const isReady = streamImgSrc.length > 0;

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
        interactive={interactive}
        userIsControlling={userIsControlling}
        setUserIsControlling={setUserIsControlling}
        inputReady={inputReady}
        containerRef={containerRef}
        showControlButtons={showControlButtons}
        handlers={handlers}
        currentUrl={currentUrl}
      />
    );
  }

  return <StreamStatusPanel diagnostic={diagnostic} />;
}

export { BrowserSessionStream };
