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
import {
  useRecordingStore,
  countVisibleDraftSteps,
} from "@/store/useRecordingStore";
import { useSettingsStore } from "@/store/SettingsStore";
import { wssBaseUrl, newWssBaseUrl } from "@/util/env";
import { formatRecordingClock } from "@/util/recordingClock";
import { installNoVncGestureCrashGuard } from "@/util/novncGestureCrashGuard";
import { cn } from "@/util/utils";
import {
  StreamStatusPanel,
  type StreamDiagnostic,
} from "@/routes/streaming/StreamDiagnostics";
import {
  handleVncClipboardPasteShortcut,
  type HeldMetaSides,
} from "@/components/browserStreamClipboard";
import { useRecordingMessageChannel } from "@/routes/streaming/useRecordingMessageChannel";
import { useWebSocketParams } from "@/routes/streaming/webSocketParams";

import "./browser-stream.css";

installNoVncGestureCrashGuard();

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

type Props = {
  browserSessionId?: string;
  exfiltrate?: boolean;
  interactive?: boolean;
  showControlButtons?: boolean;
  // Whether unmounting clears the recording store. The studio passes false: it
  // remounts this component across CDP<->VNC swaps without the session ending.
  resetRecordingOnUnmount?: boolean;
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
  resetRecordingOnUnmount = true,
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
  const [vncDisconnectedTrigger, setVncDisconnectedTrigger] = useState(0);
  const [isVncConnected, setIsVncConnected] = useState<boolean>(false);
  const [isCanvasReady, setIsCanvasReady] = useState<boolean>(false);
  const [terminalDiagnostic, setTerminalDiagnostic] =
    useState<StreamDiagnostic | null>(null);
  const [isReady, setIsReady] = useState(false);
  const [messagesDisconnectedTrigger, setMessagesDisconnectedTrigger] =
    useState(0);
  const prevMessageConnectedRef = useRef<boolean>(false);
  const [canvasContainer, setCanvasContainer] = useState<HTMLDivElement | null>(
    null,
  );
  const setCanvasContainerRef = useCallback((node: HTMLDivElement | null) => {
    setCanvasContainer(node);
  }, []);
  const rfbRef = useRef<RFB | null>(null);
  const onActivityRef = useRef(onActivity);
  const userCanSendVncInputRef = useRef(false);
  const heldMetaSidesRef = useRef<HeldMetaSides>({
    left: false,
    right: false,
  });
  const observerRef = useRef<MutationObserver | null>(null);
  const messageReconnectAttemptsRef = useRef(0);
  const messageReconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const isRecording = useRecordingStore((state) => state.isRecording);
  const workflowPermanentId = useRecordingStore(
    (state) => state.workflowPermanentId,
  );
  const settingsStore = useSettingsStore();
  const credentialGetter = useCredentialGetter();
  const getWebSocketParams = useWebSocketParams();
  const isBrowserSessionAvailable =
    entity !== "browserSession" || hasBrowserSession;
  const isBrowserSessionBackendReady =
    entity !== "browserSession" || isBrowserSessionStarted;
  const getFrameDataUrl = useCallback(() => {
    const canvas = canvasContainer?.querySelector("canvas");
    return canvas?.toDataURL("image/jpeg", 0.5) ?? null;
  }, [canvasContainer]);
  const handleMessageConnectionChange = useCallback(
    (connected: boolean, event?: CloseEvent) => {
      if (connected) {
        setTerminalDiagnostic(null);
        return;
      }
      if (!event) {
        return;
      }
      const { code, reason } = event;
      setTerminalDiagnostic(
        (prev) =>
          prev ??
          (code === 1006
            ? {
                title: "The messages channel slipped away",
                detail: "The messages channel dropped before sending a frame.",
                hint: "Check that the API server is reachable from the UI.",
              }
            : {
                title: "The messages channel packed up and left",
                detail: `Messages channel closed with code ${code}${reason ? ` (${reason})` : ""}.`,
              }),
      );
    },
    [],
  );
  const legacyMessageSocketUrl =
    entity === "task" && runId
      ? `${wssBaseUrl}/stream/messages/task/${runId}`
      : entity === "workflow" && runId
        ? `${wssBaseUrl}/stream/messages/workflow_run/${runId}`
        : undefined;
  const { isMessageConnected, sendCommand } = useRecordingMessageChannel({
    browserSessionId: runId,
    enabled:
      showStream &&
      Boolean(canvasContainer) &&
      Boolean(runId) &&
      isBrowserSessionAvailable &&
      isBrowserSessionBackendReady,
    exfiltrate,
    workflowPermanentId,
    getFrameDataUrl,
    clipboard: "vnc",
    socketUrl: legacyMessageSocketUrl,
    reconnectTrigger: messagesDisconnectedTrigger,
    onConnectionChange: handleMessageConnectionChange,
  });

  useEffect(() => {
    onActivityRef.current = onActivity;
  }, [onActivity]);

  useEffect(() => {
    setIsBrowserSessionStarted(false);
    setIsReady(false);
    setIsVncConnected(false);
    setIsCanvasReady(false);
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
      let didDisconnect = false;

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
          if (cancelled || didDisconnect) return;
          didDisconnect = true;
          setIsVncConnected(false);
          setIsCanvasReady(false);
          setVncDisconnectedTrigger((x) => x + 1);
          onClose?.();
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

  // effect to send a message when the user is controlling, vs not controlling
  useEffect(() => {
    if (!isMessageConnected) {
      return;
    }

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
      // Track only Meta keydowns noVNC's canvas receives: restoring a side noVNC never tracked would strand the modifier remotely, since noVNC drops keyups for keys it never saw down.
      if (event.key === "Meta" && event.target instanceof HTMLCanvasElement) {
        if (event.code === "MetaLeft") {
          heldMetaSidesRef.current = {
            ...heldMetaSidesRef.current,
            left: true,
          };
        } else if (event.code === "MetaRight") {
          heldMetaSidesRef.current = {
            ...heldMetaSidesRef.current,
            right: true,
          };
        }
      }

      if (!userCanSendVncInputRef.current) {
        return;
      }

      void handleVncClipboardPasteShortcut(event, rfbRef.current, {
        getHeldMetaSides: () => heldMetaSidesRef.current,
        onPasteError: () => {
          toast({
            title: "Paste failed",
            description:
              "Skyvern couldn't read your clipboard. Allow clipboard access for this site and try again.",
            variant: "destructive",
          });
        },
      });
    };

    const handleKeyUp = (event: KeyboardEvent) => {
      if (event.key === "Meta" && event.code === "MetaLeft") {
        heldMetaSidesRef.current = {
          ...heldMetaSidesRef.current,
          left: false,
        };
      } else if (event.key === "Meta" && event.code === "MetaRight") {
        heldMetaSidesRef.current = {
          ...heldMetaSidesRef.current,
          right: false,
        };
      }
    };

    const handleBlur = () => {
      heldMetaSidesRef.current = { left: false, right: false };
    };

    canvasContainer.addEventListener("keydown", handleKeyDown, true);
    window.addEventListener("keyup", handleKeyUp, true);
    window.addEventListener("blur", handleBlur);
    return () => {
      canvasContainer.removeEventListener("keydown", handleKeyDown, true);
      window.removeEventListener("keyup", handleKeyUp, true);
      window.removeEventListener("blur", handleBlur);
    };
  }, [canvasContainer]);

  // Read the flag through a ref so the unmount cleanup stays mount-scoped: a
  // StrictMode double-mount or transport swap must not cancel a live recording.
  const resetRecordingOnUnmountRef = useRef(resetRecordingOnUnmount);
  resetRecordingOnUnmountRef.current = resetRecordingOnUnmount;
  // By default an unmount means the user abandoned the session, and the reset
  // keeps stale isRecording from leaking into the next mounted workflow as a
  // stuck recording panel. Surfaces that remount the stream while the session
  // lives on (transport swaps, per-run streams) opt out and reset at the
  // session level instead.
  useEffect(() => {
    return () => {
      if (resetRecordingOnUnmountRef.current) {
        useRecordingStore.getState().reset();
      }
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

    sendCommand({ kind: "take-control" });
    setUserIsControlling(true);
  }, [isRecording, isMessageConnected, sendCommand]);

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
          // Same as InteractiveStreamView: while the take-control button is
          // offered, a click anywhere on the picture takes control instead of
          // being swallowed by this layer.
          <div
            data-testid="browser-stream-overlay"
            className={cn(
              "overlay z-10 flex items-center justify-center overflow-hidden",
              { "can-take-control": showControlButtons && !userIsControlling },
            )}
            onClick={
              showControlButtons && !userIsControlling
                ? () => setUserIsControlling(true)
                : undefined
            }
          >
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
