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

function diagnosticForStatus(status: string): StreamDiagnostic {
  switch (status) {
    case "not_found":
      return {
        title: "We've misplaced this agent run",
        detail: "The backend can't find it for your org.",
      };
    case "timeout":
      return {
        title: "The browser's gone strangely quiet",
        detail: "The run started, but no active page showed up to stream.",
        hint: "Check backend logs for browser launch errors or a streaming-mode mismatch.",
      };
    case "completed":
    case "failed":
    case "terminated":
      return {
        title: "This agent run has wrapped up",
        detail: `It's no longer live — status: ${status}.`,
      };
    default:
      return {
        title: "Waiting for browser frames",
        detail: `The stream is connected and the run status is ${status}.`,
        pending: true,
      };
  }
}

function diagnosticForClose(event: CloseEvent): StreamDiagnostic {
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
    let cancelled = false;

    async function run() {
      const credentialParam = await getCredentialParam(credentialGetter);
      if (cancelled) {
        return;
      }

      if (socketRef.current) {
        socketRef.current.close();
      }
      socketRef.current = new WebSocket(
        `${wssBaseUrl}/stream/workflow_runs/${workflowRunId}?${credentialParam}`,
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
          if (!message.screenshot && message.status) {
            setDiagnostic(diagnosticForStatus(message.status));
          }
          if (
            message.status === "completed" ||
            message.status === "failed" ||
            message.status === "terminated"
          ) {
            socketRef.current?.close();
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
        if (!cancelled && !hasFrameRef.current) {
          setDiagnostic(diagnosticForClose(event));
        }
        socketRef.current = null;
      });
    }
    run();

    return () => {
      cancelled = true;
      if (socketRef.current) {
        socketRef.current.close();
        socketRef.current = null;
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
      <div className="flex h-full w-full flex-col items-center justify-center gap-8 rounded-md bg-slate-900 py-8 text-lg">
        <span>Agent has been created.</span>
        <span>Stream will start when the agent is running.</span>
      </div>
    );
  }
  if (workflowRun?.status === Status.Queued) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-8 rounded-md bg-slate-900 py-8 text-lg">
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
