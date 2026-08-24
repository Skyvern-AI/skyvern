import { Status } from "@/api/types";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { useEffect, useRef, useState } from "react";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useFirstParam } from "@/hooks/useFirstParam";
import { getCredentialParam } from "@/util/env";
import { toast } from "@/components/ui/use-toast";
import { useQueryClient } from "@tanstack/react-query";
import { useCdpInput } from "@/routes/streaming/useCdpInput";
import { InteractiveStreamView } from "@/routes/streaming/InteractiveStreamView";
import {
  StreamStatusPanel,
  type StreamDiagnostic,
} from "@/routes/streaming/StreamDiagnostics";
import {
  STREAM_ABNORMAL_CLOSE_CODE,
  STREAM_STALE_FRAME_AFTER_ATTEMPTS,
  diagnosticForReconnectExhausted,
  diagnosticForStreamEnded,
  isStreamStatusOnlyMessage,
  isTerminalStreamStatus,
  reconnectHint,
  shouldReconnectStream,
  streamReconnectDelayMs,
} from "@/routes/streaming/streamLifecycle";
import {
  WORKFLOW_RUN_STREAM_SUBJECT,
  diagnosticForStatus,
  isWorkflowRunFinalStatus,
} from "./WorkflowRunStream.utils";

type StreamMessage = {
  task_id?: string;
  workflow_run_id?: string;
  status: string;
  screenshot?: string;
  format?: string;
  viewport_width?: number;
  viewport_height?: number;
  url?: string;
};

const STARTING_DIAGNOSTIC: StreamDiagnostic = {
  title: "Waking up the browser stream",
  detail: "Opening the stream and waiting for the first frame...",
  pending: true,
};

function diagnosticForClose(event: CloseEvent): StreamDiagnostic {
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
  alwaysShowStream?: boolean;
  interactive?: boolean;
  showControlButtons?: boolean;
  // When set, stream this run instead of the URL's (studio shell).
  workflowRunId?: string;
  // Surfaces the live page URL each frame carries (studio header).
  onUrlChange?: (url: string) => void;
  // Studio centers the frame; legacy keeps the zoomable image.
  centered?: boolean;
}

const wssBaseUrl = import.meta.env.VITE_WSS_BASE_URL;

function WorkflowRunStream({
  alwaysShowStream = false,
  interactive = false,
  showControlButtons = false,
  workflowRunId: workflowRunIdProp,
  onUrlChange,
  centered,
}: Props = {}) {
  // Held in a ref so a new callback identity doesn't reconnect the socket.
  const onUrlChangeRef = useRef(onUrlChange);
  onUrlChangeRef.current = onUrlChange;
  const urlWorkflowRunId = useFirstParam("workflowRunId", "runId");
  const workflowRunId = workflowRunIdProp ?? urlWorkflowRunId;
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery(
    workflowRunIdProp ? { workflowRunId: workflowRunIdProp } : undefined,
  );
  const [streamImgSrc, setStreamImgSrc] = useState<string>("");
  const [streamFormat, setStreamFormat] = useState<string>("png");
  const [viewportWidth, setViewportWidth] = useState(1280);
  const [viewportHeight, setViewportHeight] = useState(720);
  const [diagnostic, setDiagnostic] =
    useState<StreamDiagnostic>(STARTING_DIAGNOSTIC);
  const showStream =
    alwaysShowStream || (workflowRun && statusIsNotFinalized(workflowRun));
  const credentialGetter = useCredentialGetter();
  const workflow = workflowRun?.workflow;
  const workflowPermanentId = workflow?.workflow_permanent_id;
  const queryClient = useQueryClient();

  const socketRef = useRef<WebSocket | null>(null);
  const hasFrameRef = useRef(false);
  const reconnectAttemptsRef = useRef(0);
  const streamFinishedRef = useRef(false);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Why the stream stopped, when the server told us before closing. Survives into
  // the close handler so a reconnect notice augments that reason instead of
  // replacing it with a generic "closed with code 1000".
  const streamEndedDiagnosticRef = useRef<StreamDiagnostic | null>(null);

  const inputWsUrl =
    interactive && workflowRunId
      ? `${wssBaseUrl}/stream/cdp_input/workflow_run/${workflowRunId}`
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
    if (!showStream) {
      return;
    }
    setDiagnostic(STARTING_DIAGNOSTIC);
    hasFrameRef.current = false;
    reconnectAttemptsRef.current = 0;
    streamFinishedRef.current = false;
    streamEndedDiagnosticRef.current = null;
    let cancelled = false;

    const clearReconnectTimer = () => {
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };

    async function run() {
      const credentialParam = await getCredentialParam(credentialGetter);
      if (cancelled) {
        return;
      }

      socketRef.current?.close();
      const socket = new WebSocket(
        `${wssBaseUrl}/stream/workflow_runs/${workflowRunId}?${credentialParam}`,
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
          if (message.screenshot) {
            hasFrameRef.current = true;
            reconnectAttemptsRef.current = 0;
            streamEndedDiagnosticRef.current = null;
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
          if (message.url) {
            onUrlChangeRef.current?.(message.url);
          }
          const isTerminal = isTerminalStreamStatus(message.status);
          // A bare status frame is the server signing off. After frames have flowed
          // that ends the live view even when the status itself is non-terminal --
          // the run outlives the screencast, and rendering its last frame as
          // current is what left viewers staring at a dead browser (SKY-14617).
          const streamEnded =
            isTerminal ||
            (hasFrameRef.current && isStreamStatusOnlyMessage(message));
          if (message.status && (isTerminal || !message.screenshot)) {
            const endedDiagnostic =
              streamEnded && !isTerminal
                ? diagnosticForStreamEnded({
                    status: message.status,
                    subject: WORKFLOW_RUN_STREAM_SUBJECT,
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
            hasFrameRef.current = false;
            setStreamImgSrc("");
            // Only a terminal status forecloses reconnecting; a live run whose
            // screencast ended is exactly the case worth redialling.
            if (isTerminal) {
              streamFinishedRef.current = true;
            }
            socket.close();
          }
          // A stream-level status such as `timeout` says nothing about the run, so
          // only a genuinely final one refreshes the run's own queries.
          if (isWorkflowRunFinalStatus(message.status)) {
            queryClient.invalidateQueries({
              queryKey: ["workflowRuns"],
            });
            queryClient.invalidateQueries({
              queryKey: ["workflowRun", workflowPermanentId, workflowRunId],
            });
            queryClient.invalidateQueries({
              queryKey: ["workflowRun", workflowRunId],
            });
            queryClient.invalidateQueries({
              queryKey: ["taskWorkflowRun", workflowRunId],
            });
            queryClient.invalidateQueries({
              queryKey: ["workflowTasks", workflowRunId],
            });
            queryClient.invalidateQueries({
              queryKey: ["runs"],
            });
            if (
              message.status === "failed" ||
              message.status === "terminated"
            ) {
              toast({
                title: "Run Failed",
                description: "The agent run has failed.",
                variant: "destructive",
              });
            } else if (message.status === "completed") {
              toast({
                title: "Run Completed",
                description: "The agent run has been completed.",
                variant: "success",
              });
            }
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
            void run();
          }, delayMs);
        } else if (!cancelled && !streamFinishedRef.current) {
          // Out of retries with nothing live behind the last frame: say so instead
          // of leaving that frame up as if it were current.
          hasFrameRef.current = false;
          setStreamImgSrc("");
          setDiagnostic(
            diagnosticForReconnectExhausted(WORKFLOW_RUN_STREAM_SUBJECT),
          );
        }
      });
    }
    void run();

    return () => {
      cancelled = true;
      clearReconnectTimer();
      const socket = socketRef.current;
      if (socket) {
        socketRef.current = null;
        socket.close();
      }
    };
  }, [
    credentialGetter,
    workflowRunId,
    showStream,
    queryClient,
    workflowPermanentId,
  ]);

  const isRunningOrPaused =
    workflowRun?.status === Status.Running ||
    workflowRun?.status === Status.Paused;

  if (workflowRun?.status === Status.Created) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-8 rounded-md bg-slate-elevation1 py-8 text-lg">
        <span>Agent has been created.</span>
        <span>Stream will start when the agent is running.</span>
      </div>
    );
  }
  if (workflowRun?.status === Status.Queued) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-8 rounded-md bg-slate-elevation1 py-8 text-lg">
        <span>Your agent run is queued.</span>
        <span>Stream will start when the agent is running.</span>
      </div>
    );
  }

  if (isRunningOrPaused && streamImgSrc.length === 0) {
    return <StreamStatusPanel diagnostic={diagnostic} />;
  }

  const hasStream =
    (isRunningOrPaused || alwaysShowStream) && streamImgSrc.length > 0;

  if (hasStream) {
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
        centered={centered}
      />
    );
  }

  if (alwaysShowStream) {
    return <StreamStatusPanel diagnostic={diagnostic} />;
  }

  return null;
}

export { WorkflowRunStream };
