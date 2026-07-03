// @novnc/novnc is CJS with __esModule marker. Vite 8 (Rollup 5) changed
// CJS interop so the default import may be the namespace object instead of
// exports.default.  This guard works across bundler versions.
import _RFB, { type RfbEvent } from "@novnc/novnc/lib/rfb.js";
type RFB = _RFB;
const RFB = (_RFB as typeof _RFB & { default?: typeof _RFB }).default ?? _RFB;
import { ExitIcon, HandIcon, InfoCircledIcon } from "@radix-ui/react-icons";
import { useEffect, useMemo, useState, useRef, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { useShallow } from "zustand/react/shallow";

import { getClient } from "@/api/AxiosClient";
import {
  Status,
  type TaskApiResponse,
  type WorkflowRunStatusApiResponse,
} from "@/api/types";
import { Tip } from "@/components/Tip";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useRecordingElapsedSeconds } from "@/hooks/useRecordingElapsedSeconds";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import { buildOptimisticStep } from "@/routes/workflows/editor/recording/optimisticSteps";
import { useClientIdStore } from "@/store/useClientIdStore";
import {
  useRecordingStore,
  countVisibleDraftSteps,
  type ExfiltratedEventConsoleParams,
  type MessageInExfiltratedEvent,
  type RecordingInterpretationUpdate,
} from "@/store/useRecordingStore";
import { useSettingsStore } from "@/store/SettingsStore";
import { wssBaseUrl, newWssBaseUrl, getCredentialParam } from "@/util/env";
import { copyText } from "@/util/copyText";
import { formatRecordingClock } from "@/util/recordingClock";
import { cn } from "@/util/utils";
import {
  StreamStatusPanel,
  type StreamDiagnostic,
} from "@/routes/streaming/StreamDiagnostics";
import { handleVncClipboardPasteShortcut } from "@/components/browserStreamClipboard";

import "./browser-stream.css";

const MESSAGE_RECONNECT_DELAY_MS = 1000;
const MESSAGE_MAX_RECONNECT_ATTEMPTS = 20;
const STREAM_GAVE_UP_DIAGNOSTIC: StreamDiagnostic = {
  title: "Browser stream connection lost",
  detail:
    "The browser session stopped responding after several reconnect attempts.",
  hint: "Refresh the page to try again.",
};

interface BrowserSession {
  browser_session_id: string;
  status?: string | null;
  browser_address?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}

interface CommandBeginExfiltration {
  kind: "begin-exfiltration";
  workflow_permanent_id?: string;
  live_interpretation_enabled?: boolean;
  // Declares that this client understands delta interpretation updates, so the
  // backend may send changed_steps instead of full snapshots.
  supports_interpretation_deltas?: boolean;
  // Per-recording id: stable across reconnects, new per recording, so the
  // backend reuses the session on reconnect but starts fresh on a new recording.
  recording_attempt_id?: string;
}

interface CommandCedeControl {
  kind: "cede-control";
}

interface CommandEndExfiltration {
  kind: "end-exfiltration";
}

interface CommandTakeControl {
  kind: "take-control";
}

interface CommandRecordingCapturePause {
  kind: "recording-capture-pause";
}

interface CommandRecordingCaptureResume {
  kind: "recording-capture-resume";
}

interface CommandRecordingRearmCapture {
  kind: "recording-rearm-capture";
}

// a "Command" is an fire-n-forget out-message - it does not require a response
type Command =
  | CommandBeginExfiltration
  | CommandCedeControl
  | CommandEndExfiltration
  | CommandRecordingCapturePause
  | CommandRecordingCaptureResume
  | CommandRecordingRearmCapture
  | CommandTakeControl;

const messageInKinds = [
  "ask-for-clipboard",
  "copied-text",
  "exfiltrated-event",
  "recording-interpretation-update",
] as const;

type MessageInKind = (typeof messageInKinds)[number];

interface MessageInAskForClipboard {
  kind: "ask-for-clipboard";
}

interface MessageInCopiedText {
  kind: "copied-text";
  text: string;
}

interface MessageInRecordingInterpretationUpdate extends RecordingInterpretationUpdate {
  kind: "recording-interpretation-update";
}

type MessageIn =
  | MessageInCopiedText
  | MessageInAskForClipboard
  | MessageInExfiltratedEvent
  | MessageInRecordingInterpretationUpdate;

interface MessageOutAskForClipboardResponse {
  kind: "ask-for-clipboard-response";
  text: string;
}

type MessageOut = MessageOutAskForClipboardResponse;

type Props = {
  browserSessionId?: string;
  exfiltrate?: boolean;
  interactive?: boolean;
  showControlButtons?: boolean;
  task?: {
    run: TaskApiResponse;
  };
  workflow?: {
    run: WorkflowRunStatusApiResponse;
  };
  resizeTrigger?: number;
  isVisible?: boolean;
  isExecuting?: boolean;
  // Hide the REC pill overlay when the recording panel is visible beside the
  // stream (its header already shows the timer + step count).
  hideRecordingIndicator?: boolean;
  onReadyChange?: (isReady: boolean, browserSessionId: string | null) => void;
  onActivity?: () => void;
  // --
  onClose?: () => void;
};

type RfbWithFrameUpdates = RFB & {
  _framebufferUpdate?: () => boolean;
};

/** VNC encode settings: favor fast frames when the user is driving the browser. */
function applyVncStreamProfile(
  rfb: RFB,
  profile: "interactive" | "passive",
): void {
  if (profile === "interactive") {
    // Low CPU per frame beats max zlib compression for click/type latency.
    rfb.compressionLevel = 1;
    rfb.qualityLevel = 7;
    return;
  }
  rfb.compressionLevel = 2;
  rfb.qualityLevel = 6;
}

function RecordingPill() {
  const {
    finishRequested,
    manualCapturePaused,
    draftSteps,
    deletedStepIds,
    exposedEventCount,
    optimisticStepCount,
    interpretationEnabled,
  } = useRecordingStore(
    useShallow((state) => ({
      finishRequested: state.finishRequested,
      manualCapturePaused: state.manualCapturePaused,
      draftSteps: state.draftSteps,
      deletedStepIds: state.deletedStepIds,
      exposedEventCount: state.exposedEventCount,
      optimisticStepCount: state.optimisticSteps.length,
      interpretationEnabled: state.workflowPermanentId !== null,
    })),
  );

  const interpretedStepCount = useMemo(
    () => countVisibleDraftSteps(draftSteps, deletedStepIds),
    [draftSteps, deletedStepIds],
  );
  const elapsedSeconds = useRecordingElapsedSeconds();
  // Show interpreted + optimistic steps whenever interpretation is enabled, not
  // just after the first snapshot arrives — otherwise the first step waits a
  // backend round-trip even though the optimistic placeholder is already local.
  const count = interpretationEnabled
    ? interpretedStepCount + optimisticStepCount
    : exposedEventCount;

  const paused = manualCapturePaused && !finishRequested;

  return (
    <div
      className={cn(
        "inline-flex h-6 items-center gap-2 rounded-full border px-3 text-xs font-semibold tabular-nums",
        paused
          ? "border-amber-500/50 bg-amber-950 text-amber-200"
          : "border-red-500/50 bg-red-950 text-red-200",
      )}
    >
      <span
        className={cn("h-2 w-2 rounded-full", {
          "bg-amber-500": paused,
          "bg-red-500": !paused,
          "animate-pulse": !finishRequested && !paused,
          "opacity-50": finishRequested,
        })}
      />
      {finishRequested ? "FINISHING" : paused ? "PAUSED" : "REC"}{" "}
      {formatRecordingClock(elapsedSeconds)}
      <span className={paused ? "text-amber-400/80" : "text-red-400/80"}>
        ·
      </span>
      {count}
    </div>
  );
}

function BrowserStream({
  browserSessionId = undefined,
  exfiltrate = false,
  interactive = true,
  showControlButtons = undefined,
  task = undefined,
  workflow = undefined,
  resizeTrigger,
  isVisible = true,
  isExecuting = false,
  hideRecordingIndicator = false,
  onReadyChange,
  onActivity,
  // --
  onClose,
}: Props) {
  let showStream: boolean = false;
  let runId: string | null;
  let entity: "browserSession" | "task" | "workflow" | null;

  if (browserSessionId) {
    runId = browserSessionId;
    entity = "browserSession";
    showStream = true;
  } else if (task) {
    runId = task.run.task_id;
    showStream = statusIsNotFinalized(task.run);
    entity = "task";
  } else if (workflow) {
    runId = workflow.run.workflow_run_id;
    browserSessionId = workflow.run.browser_session_id ?? undefined;
    showStream = statusIsNotFinalized(workflow.run);
    entity = "workflow";
  } else {
    entity = null;
    runId = null;
  }

  useQuery({
    queryKey: ["hasBrowserSession", browserSessionId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");

      try {
        const response = await client.get<BrowserSession | null>(
          `/browser_sessions/${browserSessionId}`,
        );
        const browserSession = response.data;

        if (!browserSession || browserSession.completed_at) {
          setHasBrowserSession(false);
          setIsBrowserSessionStarted(false);
          return false;
        }

        setHasBrowserSession(true);
        const sessionStarted = Boolean(
          browserSession.started_at || browserSession.browser_address,
        );
        setIsBrowserSessionStarted(sessionStarted);
        return sessionStarted;
      } catch (error) {
        setHasBrowserSession(false);
        setIsBrowserSessionStarted(false);
        return false;
      }
    },
    enabled: entity === "browserSession" && !!browserSessionId,
    refetchInterval: (query) => (query.state.data ? 5000 : 1000),
  });

  const [hasBrowserSession, setHasBrowserSession] = useState(true); // be optimistic
  const [isBrowserSessionStarted, setIsBrowserSessionStarted] = useState(false);
  const [userIsControlling, setUserIsControlling] = useState(false);
  const [messageSocket, setMessageSocket] = useState<WebSocket | null>(null);
  const [vncDisconnectedTrigger, setVncDisconnectedTrigger] = useState(0);
  const prevVncConnectedRef = useRef<boolean>(false);
  const [isVncConnected, setIsVncConnected] = useState<boolean>(false);
  const [isCanvasReady, setIsCanvasReady] = useState<boolean>(false);
  const [terminalDiagnostic, setTerminalDiagnostic] =
    useState<StreamDiagnostic | null>(null);
  const [isReady, setIsReady] = useState(false);
  const [messagesDisconnectedTrigger, setMessagesDisconnectedTrigger] =
    useState(0);
  const prevMessageConnectedRef = useRef<boolean>(false);
  const [isMessageConnected, setIsMessageConnected] = useState<boolean>(false);
  const [canvasContainer, setCanvasContainer] = useState<HTMLDivElement | null>(
    null,
  );
  const setCanvasContainerRef = useCallback((node: HTMLDivElement | null) => {
    setCanvasContainer(node);
  }, []);
  const rfbRef = useRef<RFB | null>(null);
  const onActivityRef = useRef(onActivity);
  const userCanSendVncInputRef = useRef(false);
  const observerRef = useRef<MutationObserver | null>(null);
  const messageReconnectAttemptsRef = useRef(0);
  const messageReconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const clientId = useClientIdStore((state) => state.clientId);
  const isRecording = useRecordingStore((state) => state.isRecording);
  const settingsStore = useSettingsStore();
  const credentialGetter = useCredentialGetter();
  const isBrowserSessionAvailable =
    entity !== "browserSession" || hasBrowserSession;
  const isBrowserSessionBackendReady =
    entity !== "browserSession" || isBrowserSessionStarted;

  useEffect(() => {
    onActivityRef.current = onActivity;
  }, [onActivity]);

  useEffect(() => {
    setIsBrowserSessionStarted(false);
    setIsReady(false);
    setIsVncConnected(false);
    setIsCanvasReady(false);
    setIsMessageConnected(false);
    setHasBrowserSession(true);
    setTerminalDiagnostic(null);
    messageReconnectAttemptsRef.current = 0;
    if (messageReconnectTimerRef.current) {
      clearTimeout(messageReconnectTimerRef.current);
      messageReconnectTimerRef.current = null;
    }
    if (rfbRef.current) {
      rfbRef.current.disconnect();
      rfbRef.current = null;
    }
  }, [browserSessionId]);

  const getWebSocketParams = useCallback(async () => {
    const params = new URLSearchParams(
      await getCredentialParam(credentialGetter),
    );
    params.set("client_id", clientId);
    return params.toString();
  }, [clientId, credentialGetter]);

  // browser is ready
  useEffect(() => {
    setIsReady(
      isVncConnected &&
        isCanvasReady &&
        isBrowserSessionAvailable &&
        isBrowserSessionBackendReady,
    );
  }, [
    isBrowserSessionAvailable,
    isBrowserSessionBackendReady,
    isCanvasReady,
    isVncConnected,
  ]);

  useEffect(() => {
    // browserSessionId intentionally not a dep: re-firing on prop change
    // before isReady resets would spuriously report (true, newSessionId).
    onReadyChange?.(isReady, isReady ? (browserSessionId ?? null) : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isReady, onReadyChange]);

  useEffect(() => {
    return () => {
      onReadyChange?.(false, null);
    };
  }, [onReadyChange]);

  // `isUsingABrowser` is tied to local `isReady`, so this component owns it.
  // `isLoadingABrowser` is owned by the route instead (SKY-9777).
  useEffect(() => {
    settingsStore.setIsUsingABrowser(isReady);
    settingsStore.setBrowserSessionId(
      isReady ? (browserSessionId ?? null) : null,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isReady, browserSessionId]);

  // effect for vnc disconnects only
  useEffect(() => {
    if (prevVncConnectedRef.current && !isVncConnected) {
      setVncDisconnectedTrigger((x) => x + 1);
      onClose?.();
    }
    prevVncConnectedRef.current = isVncConnected;
  }, [isVncConnected, onClose]);

  // message channel reconnect policy
  useEffect(() => {
    const messageJustClosed =
      prevMessageConnectedRef.current && !isMessageConnected;
    prevMessageConnectedRef.current = isMessageConnected;

    if (isMessageConnected) {
      return;
    }

    // A live VNC stream proves the session is real: reconnect now and drop the cap (also recovers a late VNC connect).
    if (isVncConnected) {
      messageReconnectAttemptsRef.current = 0;
      if (messageReconnectTimerRef.current) {
        clearTimeout(messageReconnectTimerRef.current);
        messageReconnectTimerRef.current = null;
      }
      setMessagesDisconnectedTrigger((x) => x + 1);
      return;
    }

    if (!messageJustClosed) {
      return;
    }

    // No stream is live; a session the backend can't find would respin forever, so cap it.
    if (messageReconnectAttemptsRef.current >= MESSAGE_MAX_RECONNECT_ATTEMPTS) {
      setTerminalDiagnostic((prev) => prev ?? STREAM_GAVE_UP_DIAGNOSTIC);
      return;
    }

    messageReconnectAttemptsRef.current += 1;
    if (messageReconnectTimerRef.current) {
      clearTimeout(messageReconnectTimerRef.current);
    }
    messageReconnectTimerRef.current = setTimeout(() => {
      messageReconnectTimerRef.current = null;
      setMessagesDisconnectedTrigger((x) => x + 1);
    }, MESSAGE_RECONNECT_DELAY_MS);
  }, [isMessageConnected, isVncConnected]);

  useEffect(() => {
    return () => {
      if (messageReconnectTimerRef.current) {
        clearTimeout(messageReconnectTimerRef.current);
      }
    };
  }, []);

  // The low-latency encode profile is scoped to recording: that's where frame
  // lag directly delays draft feedback. Other interactive live-browser streams
  // keep the default profile to avoid a broad bandwidth/CPU bump.
  const vncInteractive = exfiltrate;

  // vnc socket
  useEffect(
    () => {
      if (!showStream || !canvasContainer || !runId) {
        if (rfbRef.current) {
          rfbRef.current.disconnect();
          rfbRef.current = null;
          setIsVncConnected(false);
        }
        return;
      }

      let cancelled = false;

      async function setupVnc() {
        if (rfbRef.current && isVncConnected) {
          return;
        }

        const wsParams = await getWebSocketParams();
        if (cancelled) {
          return;
        }
        const vncUrl =
          entity === "browserSession"
            ? `${newWssBaseUrl}/stream/vnc/browser_session/${runId}?${wsParams}`
            : entity === "task"
              ? `${wssBaseUrl}/stream/vnc/task/${runId}?${wsParams}`
              : entity === "workflow"
                ? `${wssBaseUrl}/stream/vnc/workflow_run/${runId}?${wsParams}`
                : null;

        if (!vncUrl) {
          throw new Error("No vnc url");
        }

        if (rfbRef.current) {
          rfbRef.current.disconnect();
        }

        if (!isBrowserSessionAvailable || !isBrowserSessionBackendReady) {
          setIsVncConnected(false);
          return;
        }

        const canvas = canvasContainer;

        if (!canvas) {
          throw new Error("Canvas element not found");
        }

        observerRef.current = new MutationObserver(() => {
          const canvasElement = canvasContainer.querySelector("canvas");
          if (canvasElement) {
            setIsCanvasReady(true);
            observerRef.current?.disconnect();
          }
        });

        observerRef.current.observe(canvasContainer, {
          childList: true,
          subtree: true,
        });

        const rfb = new RFB(canvas, vncUrl);

        rfb.scaleViewport = true;
        applyVncStreamProfile(rfb, vncInteractive ? "interactive" : "passive");

        const frameUpdateRfb = rfb as RfbWithFrameUpdates;
        // noVNC does not expose a public framebuffer-update event in 1.5.x.
        // Hook the internal method defensively so activity tracking degrades
        // to no-op if the private API changes.
        const originalFrameUpdate =
          frameUpdateRfb._framebufferUpdate?.bind(rfb);
        if (originalFrameUpdate) {
          frameUpdateRfb._framebufferUpdate = () => {
            const didCompleteFrameUpdate = originalFrameUpdate();
            if (didCompleteFrameUpdate) {
              onActivityRef.current?.();
            }
            return didCompleteFrameUpdate;
          };
        }

        rfbRef.current = rfb;

        const canvasElement = canvasContainer.querySelector("canvas");

        if (canvasElement) {
          setIsCanvasReady(true);
          observerRef.current?.disconnect();
        }

        rfb.addEventListener("connect", () => {
          setIsVncConnected(true);
          setTerminalDiagnostic(null);
          messageReconnectAttemptsRef.current = 0;
        });

        rfb.addEventListener("disconnect", (e: RfbEvent) => {
          setIsVncConnected(false);
          setIsCanvasReady(false);
          if (cancelled) return;
          const clean = Boolean(e.detail?.clean);
          setTerminalDiagnostic(
            (prev) =>
              prev ??
              (clean
                ? {
                    title: "The browser stream packed up and left",
                    detail: "The browser stream closed cleanly.",
                  }
                : {
                    title: "The browser stream slipped away",
                    detail:
                      "The browser stream dropped before everything wrapped up.",
                    hint: "Refresh the page or switch to local browser streaming.",
                  }),
          );
        });
      }

      setupVnc();

      return () => {
        cancelled = true;
        if (observerRef.current) {
          observerRef.current.disconnect();
          observerRef.current = null;
        }
        if (rfbRef.current) {
          rfbRef.current.disconnect();
          rfbRef.current = null;
        }
        setIsVncConnected(false);
        setIsCanvasReady(false);
      };
    },
    // cannot include isVncConnected in deps as it will cause infinite loop
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      browserSessionId,
      entity,
      canvasContainer,
      isBrowserSessionAvailable,
      isBrowserSessionBackendReady,
      runId,
      showStream,
      vncDisconnectedTrigger, // will re-run on disconnects
    ],
  );

  // Re-apply encode profile when recording starts without tearing down the socket.
  useEffect(() => {
    if (!rfbRef.current) {
      return;
    }
    applyVncStreamProfile(
      rfbRef.current,
      vncInteractive ? "interactive" : "passive",
    );
  }, [vncInteractive]);

  useEffect(() => {
    if (!showStream || !canvasContainer || !runId) {
      return;
    }

    let ws: WebSocket | null = null;
    let cancelled = false;

    const connect = async () => {
      const wsParams = await getWebSocketParams();

      const messageUrl =
        entity === "browserSession"
          ? `${newWssBaseUrl}/stream/messages/browser_session/${runId}?${wsParams}`
          : entity === "task"
            ? `${wssBaseUrl}/stream/messages/task/${runId}?${wsParams}`
            : entity === "workflow"
              ? `${wssBaseUrl}/stream/messages/workflow_run/${runId}?${wsParams}`
              : null;

      if (!messageUrl) {
        throw new Error("No message url");
      }

      if (!isBrowserSessionAvailable || !isBrowserSessionBackendReady) {
        setIsMessageConnected(false);
        return;
      }

      ws = new WebSocket(messageUrl);

      ws.onopen = () => {
        setIsMessageConnected(true);
        setMessageSocket(ws);
        setTerminalDiagnostic(null);
      };

      ws.onmessage = (event) => {
        const data = event.data;

        try {
          const message = JSON.parse(data);

          handleMessage(message, ws);

          // handle incoming messages if needed
        } catch (e) {
          console.error(
            "Error parsing message from message channel:",
            e,
            event,
          );
        }
      };

      ws.onclose = (event) => {
        setIsMessageConnected(false);
        setMessageSocket(null);
        if (cancelled) return;
        const { code, reason } = event;
        setTerminalDiagnostic(
          (prev) =>
            prev ??
            (code === 1006
              ? {
                  title: "The messages channel slipped away",
                  detail:
                    "The messages channel dropped before sending a frame.",
                  hint: "Check that the API server is reachable from the UI.",
                }
              : {
                  title: "The messages channel packed up and left",
                  detail: `Messages channel closed with code ${code}${reason ? ` (${reason})` : ""}.`,
                }),
        );
      };
    };

    connect();

    return () => {
      cancelled = true;
      try {
        ws && ws.close();
      } catch (e) {
        // pass
      }
    };
    // NOTE: adding getWebSocketParams causes constant reconnects of message channel when,
    // for instance, take-control or cede-control is clicked
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    browserSessionId,
    canvasContainer,
    messagesDisconnectedTrigger,
    entity,
    isBrowserSessionAvailable,
    isBrowserSessionBackendReady,
    runId,
    showStream,
  ]);

  // effect to send a message when the user is controlling, vs not controlling
  useEffect(() => {
    if (!isMessageConnected) {
      return;
    }

    const sendCommand = (command: Command) => {
      if (!messageSocket) {
        return;
      }

      messageSocket.send(JSON.stringify(command));
    };

    if (interactive || userIsControlling) {
      sendCommand({ kind: "take-control" });
    } else {
      sendCommand({ kind: "cede-control" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interactive, isMessageConnected, userIsControlling]);

  // noVNC (1.5.0) only rescales via its own observer, which gets swallowed on
  // re-parent; re-asserting scaleViewport on resize forces a recompute (skip 0×0).
  useEffect(() => {
    if (!canvasContainer || typeof ResizeObserver === "undefined") {
      return;
    }
    const rescale = () => {
      const rect = canvasContainer.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0 && rfbRef.current) {
        rfbRef.current.scaleViewport = true;
      }
    };
    rescale();
    const observer = new ResizeObserver(rescale);
    observer.observe(canvasContainer);
    return () => observer.disconnect();
  }, [canvasContainer, resizeTrigger]);

  // Effect to show toast when task or workflow reaches a final state based on hook updates
  useEffect(() => {
    const run = task ? task.run : workflow ? workflow.run : null;

    if (!run) {
      return;
    }

    const name = task ? "task" : workflow ? "agent" : null;

    if (!name) {
      return;
    }

    if (run.status === Status.Failed || run.status === Status.Terminated) {
      // Only show toast if VNC is not connected or was never connected,
      // to avoid double toasting if disconnect handler also triggers similar logic.
      // However, the disconnect handler now primarily invalidates queries.
      toast({
        title: "Run Ended",
        description: `The ${name} run has ${run.status}.`,
        variant: "destructive",
      });
    } else if (run.status === Status.Completed) {
      toast({
        title: "Run Completed",
        description: `The ${name} run has been completed.`,
        variant: "success",
      });
    }
  }, [task, workflow]);

  const { workflowPermanentId, recordingAttemptId } = useRecordingStore(
    useShallow((state) => ({
      workflowPermanentId: state.workflowPermanentId,
      recordingAttemptId: state.recordingAttemptId,
    })),
  );

  // effect for exfiltration
  useEffect(() => {
    const sendCommand = (command: Command) => {
      if (!messageSocket) {
        return;
      }

      messageSocket.send(JSON.stringify(command));
    };

    if (exfiltrate) {
      // Including a workflow id turns on backend live interpretation: the
      // server streams recording-interpretation-update draft step snapshots
      // back over this same socket while the user records.
      sendCommand({
        kind: "begin-exfiltration",
        workflow_permanent_id: workflowPermanentId ?? undefined,
        live_interpretation_enabled: Boolean(workflowPermanentId),
        // Client capability: the backend only emits per-step delta updates
        // (changed_steps upserted by step_id) to clients that declare support;
        // otherwise it falls back to full snapshots.
        supports_interpretation_deltas: true,
        // Stable across reconnects of this recording; a new recording mints a
        // new id so the backend starts a fresh interpretation session.
        recording_attempt_id: recordingAttemptId ?? undefined,
      });
    } else {
      sendCommand({ kind: "end-exfiltration" });
    }
  }, [exfiltrate, messageSocket, workflowPermanentId, recordingAttemptId]);

  const manualCapturePaused = useRecordingStore(
    (state) => state.manualCapturePaused,
  );
  const draftEditDepth = useRecordingStore((state) => state.draftEditDepth);
  const capturePaused = manualCapturePaused || draftEditDepth > 0;
  const previousCapturePausedRef = useRef(false);

  // Pause exfiltration + live interpretation while the operator edits drafts
  // or explicitly pauses capture.
  useEffect(() => {
    if (!exfiltrate || !messageSocket) {
      // Backend pause state is per exfiltration session, so start the next
      // session's edge detection from "not paused". A recording that ended
      // while paused would otherwise fire a spurious resume on the next start;
      // and if capture IS paused on a mid-recording reconnect, this re-sends
      // the pause to the new socket (idempotent) instead of assuming it.
      previousCapturePausedRef.current = false;
      return;
    }

    const wasPaused = previousCapturePausedRef.current;
    if (!wasPaused && capturePaused) {
      messageSocket.send(JSON.stringify({ kind: "recording-capture-pause" }));
    } else if (wasPaused && !capturePaused) {
      messageSocket.send(JSON.stringify({ kind: "recording-capture-resume" }));
    }

    previousCapturePausedRef.current = capturePaused;
  }, [capturePaused, exfiltrate, messageSocket]);

  useEffect(() => {
    if (!interactive) {
      setUserIsControlling(false);
    }
  }, [interactive]);

  // When control can no longer be offered (buttons hidden and not inherently
  // interactive), a prior grab must be released or its input keeps flowing.
  // Recording is exempt: it holds take-control for exfiltration.
  useEffect(() => {
    if (!interactive && !showControlButtons && !isRecording) {
      setUserIsControlling(false);
    }
  }, [interactive, showControlButtons, isRecording]);

  const theUserIsControlling =
    userIsControlling || (interactive && !showControlButtons);

  useEffect(() => {
    userCanSendVncInputRef.current = theUserIsControlling;
  }, [theUserIsControlling]);

  useEffect(() => {
    if (!canvasContainer) {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (!userCanSendVncInputRef.current) {
        return;
      }

      void handleVncClipboardPasteShortcut(event, rfbRef.current);
    };

    canvasContainer.addEventListener("keydown", handleKeyDown, true);
    return () => {
      canvasContainer.removeEventListener("keydown", handleKeyDown, true);
    };
  }, [canvasContainer]);

  // Reset the recording store when the stream unmounts — including mid-recording.
  // The stream never unmounts during a live recording flow (the studio keeps it
  // in a detached host across pane changes; the editor keeps it mounted while
  // recording), so an unmount means the user abandoned the session (navigated
  // away / switched workflows) and stale isRecording state would otherwise leak
  // into the next mounted workflow as a stuck recording panel.
  useEffect(() => {
    return () => {
      useRecordingStore.getState().reset();
    };
  }, []);

  // effect to ensure 'take-control' is sent on the rising edge of isRecording
  useEffect(() => {
    if (!isRecording) {
      return;
    }

    if (!isMessageConnected) {
      return;
    }

    const sendCommand = (command: Command) => {
      if (!messageSocket) {
        return;
      }

      messageSocket.send(JSON.stringify(command));
    };

    sendCommand({ kind: "take-control" });
    setUserIsControlling(true);
  }, [isRecording, isMessageConnected, messageSocket]);

  /**
   * TODO(jdo): could use zod or smth similar
   */
  const getMessage = (data: unknown): MessageIn | undefined => {
    if (!data) {
      return;
    }

    if (typeof data !== "object") {
      return;
    }

    if (!("kind" in data)) {
      return;
    }

    const k = data.kind;

    if (typeof k !== "string") {
      return;
    }

    if (!messageInKinds.includes(k as MessageInKind)) {
      return;
    }

    const kind = k as MessageInKind;

    switch (kind) {
      case "ask-for-clipboard": {
        return data as MessageInAskForClipboard;
      }
      case "copied-text": {
        if ("text" in data && typeof data.text === "string") {
          return {
            kind: "copied-text",
            text: data.text,
          };
        }
        break;
      }
      case "exfiltrated-event": {
        if (
          "event_name" in data &&
          typeof data.event_name === "string" &&
          "params" in data &&
          typeof data.params === "object" &&
          data.params !== null &&
          "source" in data &&
          typeof data.source === "string"
        ) {
          const event = data as MessageInExfiltratedEvent;

          return {
            kind: "exfiltrated-event",
            event_name: event.event_name,
            params: event.params,
            source: event.source,
            timestamp: event.timestamp,
          } as MessageInExfiltratedEvent;
        }
        break;
      }
      case "recording-interpretation-update": {
        // steps is optional: a delta update carries changed_steps instead. Only
        // session_revision is required to accept the message.
        if (
          "session_revision" in data &&
          typeof data.session_revision === "number"
        ) {
          const update = data as MessageInRecordingInterpretationUpdate;
          return {
            kind: "recording-interpretation-update",
            interpretation_session_id:
              typeof update.interpretation_session_id === "string"
                ? update.interpretation_session_id
                : "",
            session_revision: update.session_revision,
            steps: Array.isArray(update.steps) ? update.steps : [],
            changed_steps: Array.isArray(update.changed_steps)
              ? update.changed_steps
              : [],
            // Absent/true => full snapshot (legacy). Only false triggers delta merge.
            is_snapshot:
              typeof update.is_snapshot === "boolean"
                ? update.is_snapshot
                : true,
            pending:
              typeof update.pending === "boolean" ? update.pending : false,
            finalized:
              typeof update.finalized === "boolean" ? update.finalized : false,
          };
        }
        break;
      }
      default: {
        const _exhaustive: never = kind;
        return _exhaustive;
      }
    }
  };

  // Best-effort frame grab from the VNC canvas at the moment of a recorded
  // click, so the live feed can show a zoomed shot at the action point.
  const captureRecordingScreenshot = (
    params: ExfiltratedEventConsoleParams,
  ) => {
    const schedule =
      typeof requestIdleCallback === "function"
        ? (fn: () => void) => requestIdleCallback(fn, { timeout: 750 })
        : (fn: () => void) => window.setTimeout(fn, 0);

    schedule(() => {
      try {
        const canvas = canvasContainer?.querySelector("canvas");
        if (!canvas) {
          return;
        }
        useRecordingStore.getState().addScreenshot({
          timestampMs: params.timestamp,
          dataUrl: canvas.toDataURL("image/jpeg", 0.5),
          xp: params.mousePosition.xp,
          yp: params.mousePosition.yp,
        });
      } catch {
        // toDataURL can throw on a tainted/headless canvas; shots are optional
      }
    });
  };

  const handleMessage = (data: unknown, ws: WebSocket | null) => {
    const message = getMessage(data);

    if (!message) {
      console.warn("Unknown message received on message channel:", data);
      return;
    }

    const kind = message.kind;

    const respond = (message: MessageOut) => {
      if (!ws) {
        console.warn("Cannot send message, as message socket is null.");
        console.warn(message);
        return;
      }

      ws.send(JSON.stringify(message));
    };

    switch (kind) {
      case "ask-for-clipboard": {
        if (!navigator.clipboard) {
          console.warn("Clipboard API not available.");
          return;
        }

        navigator.clipboard
          .readText()
          .then((text) => {
            toast({
              title: "Pasting Into Browser",
              description:
                "Pasting your current clipboard text into the browser.",
            });

            const response: MessageOutAskForClipboardResponse = {
              kind: "ask-for-clipboard-response",
              text,
            };

            respond(response);
          })
          .catch((err) => {
            console.error("Failed to read clipboard contents: ", err);
          });

        break;
      }
      case "copied-text": {
        const text = message.text;

        copyText(text)
          .then((success) => {
            if (success) {
              toast({
                title: "Copied to Clipboard",
                description: "The text has been copied to your clipboard.",
              });
            } else {
              toast({
                variant: "destructive",
                title: "Failed to write to Clipboard",
                description: "The text could not be copied to your clipboard.",
              });
            }
          })
          .catch((err) => {
            console.error("Failed to write to clipboard:", err);

            toast({
              variant: "destructive",
              title: "Failed to write to Clipboard",
              description: "The text could not be copied to your clipboard.",
            });
          });

        break;
      }
      case "exfiltrated-event": {
        // Read store state fresh: this handler is attached once per socket
        // and would otherwise see a stale isRecording.
        const store = useRecordingStore.getState();
        if (!store.isRecording && !store.finishRequested) {
          break;
        }
        if (store.isCapturePaused()) {
          break;
        }
        if (
          store.isRecording &&
          message.source === "console" &&
          message.params.type === "click"
        ) {
          captureRecordingScreenshot(message.params);
        }
        if (
          store.isRecording &&
          !store.finishRequested &&
          message.source === "cdp" &&
          (message.event_name === "nav:frame_navigated" ||
            message.event_name === "nav:navigated_within_document")
        ) {
          if (ws) {
            ws.send(JSON.stringify({ kind: "recording-rearm-capture" }));
          }
        }
        if (store.isRecording && !store.finishRequested) {
          const optimistic = buildOptimisticStep(message);
          if (optimistic) {
            store.addOptimisticStep(optimistic);
          }
        }
        store.add(message);
        break;
      }
      case "recording-interpretation-update": {
        useRecordingStore.getState().applyInterpretationUpdate({
          interpretation_session_id: message.interpretation_session_id,
          session_revision: message.session_revision,
          steps: message.steps,
          changed_steps: message.changed_steps,
          is_snapshot: message.is_snapshot,
          pending: message.pending,
          finalized: message.finalized,
        });
        break;
      }
      default: {
        const _exhaustive: never = kind;
        return _exhaustive;
      }
    }
  };

  const streamDiagnostic: StreamDiagnostic =
    !showStream || !runId
      ? {
          title: "Starting browser session",
          detail: "Waiting for a live browser session to attach.",
        }
      : entity === "browserSession" && browserSessionId && !hasBrowserSession
        ? {
            title: "This browser session has wandered off",
            detail: "Looks like it slipped away mid-stream.",
            hint: "Refresh the page or spin up a fresh browser session.",
          }
        : terminalDiagnostic
          ? terminalDiagnostic
          : !isBrowserSessionBackendReady
            ? {
                title: "Warming up your browser",
                detail:
                  "The session is here — we're just waiting for the backend to give the green light.",
                pending: true,
              }
            : !isVncConnected
              ? {
                  title: "Reaching out to your browser",
                  detail: "Opening up the live stream and message channels...",
                  hint: "If this sticks around, check VNC support for the session or switch to local browser streaming.",
                  pending: true,
                }
              : !isCanvasReady
                ? {
                    title: "Setting the stage",
                    detail:
                      "The connection is open — now we're waiting for the browser to paint its first frame.",
                    pending: true,
                  }
                : {
                    title: "Tuning in to your browser...",
                    pending: true,
                  };

  return (
    <>
      <div
        className={cn(
          "browser-stream relative flex flex-col items-center justify-center",
          {
            "user-is-controlling": theUserIsControlling,
          },
        )}
        ref={setCanvasContainerRef}
      >
        {isReady && isVisible && (
          <div className="overlay z-10 flex items-center justify-center overflow-hidden">
            {showControlButtons && (
              <div className="control-buttons pointer-events-none relative flex h-full w-full items-center justify-center">
                <Button
                  onClick={() => {
                    setUserIsControlling(true);
                  }}
                  className={cn("control-button pointer-events-auto border", {
                    hide: userIsControlling,
                  })}
                  size="sm"
                >
                  <HandIcon className="mr-2 h-4 w-4" />
                  take control
                </Button>
                <Button
                  onClick={() => {
                    setUserIsControlling(false);
                  }}
                  className={cn(
                    "control-button pointer-events-auto absolute bottom-0 border",
                    {
                      hide: !userIsControlling,
                    },
                  )}
                  size="sm"
                >
                  <ExitIcon className="mr-2 h-4 w-4" />
                  stop controlling
                </Button>
              </div>
            )}
          </div>
        )}
        {isRecording && (
          <div className="pointer-events-none absolute flex aspect-video w-full items-center justify-center rounded-xl p-2 outline outline-8 outline-offset-[-2px] outline-red-500 animate-in fade-in">
            {/* The pill duplicates the recording panel's header (timer + step
                count), so it's hidden while the panel is visible alongside. */}
            {!hideRecordingIndicator && (
              <div className="relative h-full w-full">
                <div className="pointer-events-auto absolute top-[-3rem] flex w-full items-center justify-start gap-2">
                  <RecordingPill />
                  <Tip content="Your actions appear as blocks in the recording panel. Finish with Done, or use the trash icon to discard.">
                    <div className="cursor-pointer text-red-500">
                      <InfoCircledIcon />
                    </div>
                  </Tip>
                </div>
              </div>
            )}
          </div>
        )}
        {isExecuting && !isRecording && (
          <div className="pointer-events-none absolute flex aspect-video w-full animate-glow items-center justify-center rounded-xl p-2 outline outline-8 outline-offset-[-2px] outline-yellow-500">
            <div className="relative h-full w-full">
              <div className="pointer-events-auto absolute top-[-3rem] flex w-full items-center justify-start gap-2 text-yellow-500">
                <div className="truncate">Agent is working</div>
              </div>
            </div>
          </div>
        )}
        {!isReady && (
          <div className="absolute left-0 top-1/2 flex aspect-video max-h-full w-full -translate-y-1/2 flex-col items-center justify-center gap-2 rounded-md border border-neutral-200 bg-white text-sm text-neutral-600 dark:border-slate-800 dark:bg-transparent dark:text-slate-400">
            <StreamStatusPanel diagnostic={streamDiagnostic} />
          </div>
        )}
      </div>
    </>
  );
}

export { BrowserStream };
