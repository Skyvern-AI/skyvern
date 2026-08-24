import { useCallback, useEffect, useRef, useState } from "react";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { newWssBaseUrl, getCredentialParam } from "@/util/env";
import { useCdpInput } from "@/routes/streaming/useCdpInput";
import { useRecordingMessageChannel } from "@/routes/streaming/useRecordingMessageChannel";
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
  BROWSER_SESSION_STREAM_SUBJECT,
  diagnosticForStatus,
} from "./BrowserSessionStream.utils";
import {
  STREAM_ABNORMAL_CLOSE_CODE,
  STREAM_STALE_FRAME_AFTER_ATTEMPTS,
  STREAM_VNC_FALLBACK_CLOSE_CODE,
  STREAM_VNC_FALLBACK_CLOSE_REASON,
  diagnosticForReconnectExhausted,
  diagnosticForStreamEnded,
  isStreamStatusOnlyMessage,
  isTerminalStreamStatus,
  reconnectHint,
  shouldReconnectStream,
  streamReconnectDelayMs,
} from "@/routes/streaming/streamLifecycle";
import { useSettingsStore } from "@/store/SettingsStore";
import { captureRecordBrowser } from "@/util/recordBrowserTelemetry";

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

function diagnosticForClose(event: CloseEvent): StreamDiagnostic {
  if (
    event.code === STREAM_VNC_FALLBACK_CLOSE_CODE ||
    event.reason === STREAM_VNC_FALLBACK_CLOSE_REASON
  ) {
    return {
      title: "Backend wants to use VNC streaming",
      detail:
        "The UI tried local browser streaming, but the backend asked for VNC instead.",
      hint: "Check BROWSER_STREAMING_MODE on the backend and the runtime config response.",
    };
  }
  if (event.code === STREAM_ABNORMAL_CLOSE_CODE) {
    return {
      title: "The connection slipped away",
      detail: "The browser stream WebSocket dropped without closing cleanly.",
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
  exfiltrate?: boolean;
  workflowPermanentId?: string | null;
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
  exfiltrate,
  workflowPermanentId,
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
  const streamImgSrcRef = useRef("");
  const exfiltrateRef = useRef(!!exfiltrate);
  const recordingFrameCountRef = useRef(0);
  const recordingFpsSamplesRef = useRef<number[]>([]);
  const recordingHealthFlushTimerRef = useRef<number | null>(null);
  const recordingHealthEndedRef = useRef(false);
  const recordingOwnsControlRef = useRef(false);
  const previousControlExfiltrateRef = useRef(false);
  const previousInputReadyRef = useRef(false);
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
  const streamFinishedRef = useRef(false);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const recordingReconnectTimerRef = useRef<ReturnType<
    typeof setTimeout
  > | null>(null);
  const recordingChannelDisconnectedRef = useRef(false);
  const [recordingReconnectTrigger, setRecordingReconnectTrigger] = useState(0);
  // Why the stream stopped, when the server told us before closing. Survives into
  // the close handler so a reconnect notice augments that reason instead of
  // replacing it with a generic "closed with code 1000".
  const streamEndedDiagnosticRef = useRef<StreamDiagnostic | null>(null);
  exfiltrateRef.current = !!exfiltrate;

  const scheduleRecordingReconnect = useCallback(() => {
    if (recordingReconnectTimerRef.current) {
      return;
    }
    recordingReconnectTimerRef.current = setTimeout(() => {
      recordingReconnectTimerRef.current = null;
      if (exfiltrateRef.current) {
        setRecordingReconnectTrigger((trigger) => trigger + 1);
      }
    }, 1000);
  }, []);

  const handleRecordingConnectionChange = useCallback(
    (connected: boolean, event?: CloseEvent) => {
      if (connected) {
        recordingChannelDisconnectedRef.current = false;
        if (recordingReconnectTimerRef.current) {
          clearTimeout(recordingReconnectTimerRef.current);
          recordingReconnectTimerRef.current = null;
        }
        return;
      }
      if (!event) {
        return;
      }
      recordingChannelDisconnectedRef.current = true;
      if (exfiltrateRef.current) {
        scheduleRecordingReconnect();
      }
    },
    [scheduleRecordingReconnect],
  );

  useEffect(() => {
    return () => {
      if (recordingReconnectTimerRef.current) {
        clearTimeout(recordingReconnectTimerRef.current);
      }
    };
  }, []);

  const recordingChannelEnabled = exfiltrate !== undefined;
  const { isMessageConnected, sendCommand: sendRecordingCommand } =
    useRecordingMessageChannel({
      browserSessionId,
      enabled: recordingChannelEnabled,
      exfiltrate: !!exfiltrate,
      workflowPermanentId: workflowPermanentId ?? null,
      getFrameDataUrl: () =>
        streamImgSrcRef.current
          ? `data:image/${streamFormat};base64,${streamImgSrcRef.current}`
          : null,
      clipboard: "message",
      reconnectTrigger: recordingReconnectTrigger,
      onConnectionChange: handleRecordingConnectionChange,
    });

  useEffect(() => {
    if (
      exfiltrate &&
      !isMessageConnected &&
      recordingChannelDisconnectedRef.current
    ) {
      scheduleRecordingReconnect();
    }
  }, [exfiltrate, isMessageConnected, scheduleRecordingReconnect]);
  const onClipboardPaste = useCallback(
    (text: string) => {
      sendRecordingCommand({ kind: "clipboard-paste", text });
    },
    [sendRecordingCommand],
  );
  const onClipboardCopy = useCallback(() => {
    sendRecordingCommand({ kind: "clipboard-copy" });
  }, [sendRecordingCommand]);

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
    onClipboardPaste:
      exfiltrate && isMessageConnected ? onClipboardPaste : undefined,
    onClipboardCopy:
      exfiltrate && isMessageConnected ? onClipboardCopy : undefined,
  });

  useEffect(() => {
    const recordingStarted =
      !!exfiltrate && !previousControlExfiltrateRef.current;
    const recordingEnded = !exfiltrate && previousControlExfiltrateRef.current;
    const inputBecameReady = inputReady && !previousInputReadyRef.current;

    if (recordingStarted) {
      recordingOwnsControlRef.current = !userIsControlling;
      setUserIsControlling(true);
    } else if (exfiltrate && inputBecameReady) {
      setUserIsControlling(true);
    } else if (recordingEnded) {
      if (recordingOwnsControlRef.current) {
        setUserIsControlling(false);
      }
      recordingOwnsControlRef.current = false;
    }

    previousControlExfiltrateRef.current = !!exfiltrate;
    previousInputReadyRef.current = inputReady;
  }, [exfiltrate, inputReady, setUserIsControlling, userIsControlling]);

  useEffect(() => {
    onActivityRef.current = onActivity;
  }, [onActivity]);

  useEffect(() => {
    if (!exfiltrate) {
      recordingHealthEndedRef.current = true;
      return;
    }

    if (recordingHealthFlushTimerRef.current !== null) {
      if (!recordingHealthEndedRef.current) {
        window.clearTimeout(recordingHealthFlushTimerRef.current);
      }
      recordingHealthFlushTimerRef.current = null;
    }
    recordingHealthEndedRef.current = false;
    recordingFrameCountRef.current = 0;
    recordingFpsSamplesRef.current = [];
    let sampleStartedAtMs = Date.now();
    const recordSample = (ensureSample: boolean) => {
      const now = Date.now();
      const elapsedSeconds = (now - sampleStartedAtMs) / 1000;
      if (elapsedSeconds > 0) {
        recordingFpsSamplesRef.current.push(
          recordingFrameCountRef.current / elapsedSeconds,
        );
      } else if (ensureSample && recordingFpsSamplesRef.current.length === 0) {
        recordingFpsSamplesRef.current.push(0);
      }
      recordingFrameCountRef.current = 0;
      sampleStartedAtMs = now;
    };
    const interval = window.setInterval(() => recordSample(false), 30_000);

    return () => {
      window.clearInterval(interval);
      recordSample(true);
      const samples = [...recordingFpsSamplesRef.current];
      recordingFpsSamplesRef.current = [];
      const flushTimer = window.setTimeout(() => {
        captureRecordBrowser("record_browser.cdp_stream_health", {
          fps_avg:
            samples.reduce((total, sample) => total + sample, 0) /
            samples.length,
          fps_min: Math.min(...samples),
          sample_count: samples.length,
        });
        if (recordingHealthFlushTimerRef.current === flushTimer) {
          recordingHealthFlushTimerRef.current = null;
        }
      }, 0);
      recordingHealthFlushTimerRef.current = flushTimer;
    };
  }, [exfiltrate]);

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
    streamImgSrcRef.current = "";
    setStreamImgSrc("");
    setStreamFormat("png");
    setViewportWidth(1280);
    setViewportHeight(720);
    setCurrentUrl("");
    setDiagnostic(STARTING_DIAGNOSTIC);
    hasFrameRef.current = false;
    reconnectAttemptsRef.current = 0;
    streamFinishedRef.current = false;
    streamEndedDiagnosticRef.current = null;

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
            if (exfiltrateRef.current) {
              recordingFrameCountRef.current += 1;
            }
            hasFrameRef.current = true;
            reconnectAttemptsRef.current = 0;
            streamEndedDiagnosticRef.current = null;
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
                streamImgSrcRef.current = pending.screenshot;
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
          // A bare status frame is the server signing off. After frames have flowed
          // that ends the live view even when the status itself is non-terminal --
          // the session outlives the screencast, and rendering its last frame as
          // current is what left viewers staring at a dead browser (SKY-14617).
          const streamEnded =
            isTerminal ||
            (hasFrameRef.current && isStreamStatusOnlyMessage(message));
          if (message.status && (isTerminal || !message.screenshot)) {
            const endedDiagnostic =
              streamEnded && !isTerminal
                ? diagnosticForStreamEnded({
                    status: message.status,
                    subject: BROWSER_SESSION_STREAM_SUBJECT,
                  })
                : diagnosticForStatus(message.status);
            streamEndedDiagnosticRef.current = streamEnded
              ? endedDiagnostic
              : null;
            setDiagnostic(endedDiagnostic);
          }
          if (streamEnded) {
            // Drop the last frame: keeping it leaves a dead, still-interactive
            // screenshot covering the status panel.
            clearPendingFrame();
            hasFrameRef.current = false;
            streamImgSrcRef.current = "";
            setStreamImgSrc("");
            // Only a terminal status forecloses reconnecting; a live session whose
            // screencast ended is exactly the case worth redialling.
            if (isTerminal) {
              streamFinishedRef.current = true;
            }
            socket.close();
          }
        } catch (e) {
          console.error("Failed to parse message", e);
          // The backend only sends non-JSON text to reject credentials, and
          // retrying that would just burn the reconnect budget in silence.
          streamFinishedRef.current = true;
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

        // Prefer the reason the server gave over "the socket closed": after a clean
        // close those are the same event, and only the former says anything useful.
        const closeDiagnostic =
          streamEndedDiagnosticRef.current ?? diagnosticForClose(event);
        if (!cancelled && !hasFrameRef.current && !streamFinishedRef.current) {
          setDiagnostic(closeDiagnostic);
        }
        const canReconnect =
          !cancelled &&
          shouldReconnectStream({
            closeCode: event.code,
            closeReason: event.reason,
            streamFinished: streamFinishedRef.current,
            reconnectAttempts: reconnectAttemptsRef.current,
          });

        if (canReconnect) {
          const delayMs = streamReconnectDelayMs(reconnectAttemptsRef.current);
          reconnectAttemptsRef.current += 1;
          if (
            hasFrameRef.current &&
            reconnectAttemptsRef.current > STREAM_STALE_FRAME_AFTER_ATTEMPTS
          ) {
            hasFrameRef.current = false;
            streamImgSrcRef.current = "";
            setStreamImgSrc("");
          }
          if (!hasFrameRef.current) {
            setDiagnostic({
              ...closeDiagnostic,
              hint: reconnectHint(reconnectAttemptsRef.current),
            });
          }
          clearReconnectTimer();
          reconnectTimerRef.current = setTimeout(() => {
            reconnectTimerRef.current = null;
            void connect();
          }, delayMs);
        } else if (
          !cancelled &&
          !streamFinishedRef.current &&
          // A transport switch is the caller's cue to re-mount on VNC, not a drop.
          event.code !== STREAM_VNC_FALLBACK_CLOSE_CODE &&
          event.reason !== STREAM_VNC_FALLBACK_CLOSE_REASON
        ) {
          // Out of retries with nothing live behind the last frame: say so instead
          // of leaving that frame up as if it were current.
          hasFrameRef.current = false;
          streamImgSrcRef.current = "";
          setStreamImgSrc("");
          setDiagnostic(
            diagnosticForReconnectExhausted(BROWSER_SESSION_STREAM_SUBJECT),
          );
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
        showControlButtons={showControlButtons && !exfiltrate}
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
