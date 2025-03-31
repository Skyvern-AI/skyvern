import { Status } from "@/api/types";
import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";
import { ZoomableImage } from "@/components/ZoomableImage";
import { useEffect, useState } from "react";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useParams } from "react-router-dom";
import { envCredential } from "@/util/env";
import { toast } from "@/components/ui/use-toast";
import { useQueryClient } from "@tanstack/react-query";

type StreamMessage = {
  task_id: string;
  status: string;
  screenshot?: string;
};

let socket: WebSocket | null = null;

const wssBaseUrl = import.meta.env.VITE_WSS_BASE_URL;

function WorkflowRunStream() {
  const { data: workflowRun } = useWorkflowRunQuery();
  const [streamImgSrc, setStreamImgSrc] = useState<string>("");
  const showStream = workflowRun && statusIsNotFinalized(workflowRun);
  const credentialGetter = useCredentialGetter();
  const { workflowRunId, workflowPermanentId } = useParams();
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!showStream) {
      return;
    }

    async function run() {
      // Create WebSocket connection.
      let credential = null;
      if (credentialGetter) {
        const token = await credentialGetter();
        credential = `?token=Bearer ${token}`;
      } else {
        credential = `?apikey=${envCredential}`;
      }
      if (socket) {
        socket.close();
      }
      socket = new WebSocket(
        `${wssBaseUrl}/stream/workflow_runs/${workflowRunId}${credential}`,
      );
      // Listen for messages
      socket.addEventListener("message", (event) => {
        try {
          const message: StreamMessage = JSON.parse(event.data);
          if (message.screenshot) {
            setStreamImgSrc(message.screenshot);
          }
          if (
            message.status === "completed" ||
            message.status === "failed" ||
            message.status === "terminated"
          ) {
            socket?.close();
            queryClient.invalidateQueries({
              queryKey: ["workflowRuns"],
            });
            queryClient.invalidateQueries({
              queryKey: ["workflowRun", workflowPermanentId, workflowRunId],
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

      socket.addEventListener("close", () => {
        socket = null;
      });
    }
    run();

    return () => {
      if (socket) {
        socket.close();
        socket = null;
      }
    };
  }, [
    credentialGetter,
    workflowRunId,
    showStream,
    queryClient,
    workflowPermanentId,
  ]);

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

  if (workflowRun?.status === Status.Running && streamImgSrc.length === 0) {
    return (
      <div className="flex h-full w-full items-center justify-center rounded-md bg-slate-900 py-8 text-lg">
        Starting the stream...
      </div>
    );
  }

  if (workflowRun?.status === Status.Running && streamImgSrc.length > 0) {
    return (
      <div className="h-full w-full">
        <ZoomableImage
          src={`data:image/png;base64,${streamImgSrc}`}
          className="rounded-md"
        />
      </div>
    );
  }
  return null;
}

export { WorkflowRunStream };
