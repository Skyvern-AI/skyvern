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

type StreamMessage = {
  task_id?: string;
  workflow_run_id?: string;
  status: string;
  screenshot?: string;
  format?: string;
  viewport_width?: number;
  viewport_height?: number;
};

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
  const showStream =
    alwaysShowStream || (workflowRun && statusIsNotFinalized(workflowRun));
  const credentialGetter = useCredentialGetter();
  const workflow = workflowRun?.workflow;
  const workflowPermanentId = workflow?.workflow_permanent_id;
  const queryClient = useQueryClient();

  const socketRef = useRef<WebSocket | null>(null);

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

    async function run() {
      const credentialParam = await getCredentialParam(credentialGetter);

      if (socketRef.current) {
        socketRef.current.close();
      }
      socketRef.current = new WebSocket(
        `${wssBaseUrl}/stream/workflow_runs/${workflowRunId}?${credentialParam}`,
      );

      socketRef.current.addEventListener("message", (event) => {
        try {
          const message: StreamMessage = JSON.parse(event.data);
          if (message.screenshot) {
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
                description: "The workflow run has failed.",
                variant: "destructive",
              });
            } else if (message.status === "completed") {
              toast({
                title: "Run Completed",
                description: "The workflow run has been completed.",
                variant: "success",
              });
            }
          }
        } catch (e) {
          console.error("Failed to parse message", e);
        }
      });

      socketRef.current.addEventListener("close", () => {
        socketRef.current = null;
      });
    }
    run();

    return () => {
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
        <span>Workflow has been created.</span>
        <span>Stream will start when the workflow is running.</span>
      </div>
    );
  }
  if (workflowRun?.status === Status.Queued) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-8 rounded-md bg-slate-900 py-8 text-lg">
        <span>Your workflow run is queued.</span>
        <span>Stream will start when the workflow is running.</span>
      </div>
    );
  }

  if (isRunningOrPaused && streamImgSrc.length === 0) {
    return (
      <div className="flex h-full w-full items-center justify-center rounded-md bg-slate-900 py-8 text-lg">
        Starting the stream...
      </div>
    );
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
      />
    );
  }

  if (alwaysShowStream) {
    return (
      <div className="flex h-full w-full items-center justify-center">
        Waiting for stream...
      </div>
    );
  }

  return null;
}

export { WorkflowRunStream };
