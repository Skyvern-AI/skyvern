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
  title: "Starting browser stream",
  detail:
    "Opening the stream WebSocket and waiting for the first browser frame.",
};

function diagnosticForStatus(status: string): StreamDiagnostic {
  switch (status) {
    case "not_found":
      return {
        title: "Agent run not found",
        detail:
          "The backend could not find this agent run for the current organization.",
      };
    case "timeout":
      return {
        title: "Timed out waiting for browser state",
        detail:
          "The run started, but the backend did not find an active page to stream.",
        hint: "Check backend logs for browser launch errors or a streaming-mode mismatch.",
      };
    case "completed":
    case "failed":
    case "terminated":
      return {
        title: "Agent run is no longer live",
        detail: `The agent run status is ${status}.`,
      };
    default:
      return {
        title: "Waiting for browser frames",
        detail: `The stream is connected and the run status is ${status}.`,
      };
  }
}

function diagnosticForClose(event: CloseEvent): StreamDiagnostic {
  if (event.code === 1006) {
    return {
      title: "Stream connection dropped",
      detail: "The browser stream WebSocket closed before sending a frame.",
      hint: "Check that the API server is running and reachable from the UI.",
    };
  }
  return {
    title: "Stream connection closed",
    detail: `WebSocket closed with code ${event.code}${event.reason ? ` (${event.reason})` : ""}.`,
  };
}

interface Props {
  alwaysShowStream?: boolean;
  interactive?: boolean;
  showControlButtons?: boolean;
}

const wssBaseUrl = import.meta.env.VITE_WSS_BASE_URL;

function WorkflowRunStream({
  alwaysShowStream = false,
  interactive = false,
  showControlButtons = false,
}: Props = {}) {
  const workflowRunId = useFirstParam("workflowRunId", "runId");
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery();
  const [streamImgSrc, setStreamImgSrc] = useState<string>("");
  const [streamFormat, setStreamFormat] = useState<string>("png");
  const [viewportWidth, setViewportWidth] = useState(1280);
  const [viewportHeight, setViewportHeight] = useState(720);
  const [currentUrl, setCurrentUrl] = useState("");
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
    browserCommandError,
    containerRef,
    handlers,
    browserControls,
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
    setCurrentUrl("");
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
          title: "Connected to stream",
          detail: "Waiting for the backend to attach to the browser page.",
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
          if (message.url !== undefined) {
            setCurrentUrl(message.url);
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
            title: "Unexpected stream message",
            detail: "The browser stream sent a message the UI could not parse.",
          });
        }
      });

      socketRef.current.addEventListener("error", () => {
        setDiagnostic({
          title: "Stream WebSocket error",
          detail:
            "The browser stream connection hit a network or server error.",
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
        browserCommandError={interactive ? browserCommandError : null}
        containerRef={containerRef}
        showControlButtons={showControlButtons}
        handlers={handlers}
        browserControls={interactive ? browserControls : undefined}
        currentUrl={currentUrl}
      />
    );
  }

  if (alwaysShowStream) {
    return <StreamStatusPanel diagnostic={diagnostic} />;
  }

  return null;
}

export { WorkflowRunStream };
