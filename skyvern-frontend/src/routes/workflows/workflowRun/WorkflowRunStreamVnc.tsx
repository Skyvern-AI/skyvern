import { Status } from "@/api/types";
import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";
import { useEffect, useState, useRef, useCallback } from "react";
import { HandIcon, PlayIcon } from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useParams } from "react-router-dom";
import { envCredential } from "@/util/env";
import { toast } from "@/components/ui/use-toast";
import { useQueryClient } from "@tanstack/react-query";
import RFB from "@novnc/novnc/lib/rfb.js";
import { environment } from "@/util/env";
import { cn } from "@/util/utils";

import "./workflow-run-stream-vnc.css";

const wssBaseUrl = import.meta.env.VITE_WSS_BASE_URL;

function WorkflowRunStreamVnc() {
  const { data: workflowRun } = useWorkflowRunQuery();
  const { workflowRunId, workflowPermanentId } = useParams<{
    workflowRunId: string;
    workflowPermanentId: string;
  }>();

  const [userIsControlling, setUserIsControlling] = useState<boolean>(false);
  const [vncDisconnectedTrigger, setVncDisconnectedTrigger] = useState(0);
  const prevVncConnectedRef = useRef<boolean>(false);
  const [isVncConnected, setIsVncConnected] = useState<boolean>(false);
  const showStream = workflowRun && statusIsNotFinalized(workflowRun);
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const [canvasContainer, setCanvasContainer] = useState<HTMLDivElement | null>(
    null,
  );
  const setCanvasContainerRef = useCallback((node: HTMLDivElement | null) => {
    setCanvasContainer(node);
  }, []);

  const rfbRef = useRef<RFB | null>(null);

  // effect for disconnects only
  useEffect(() => {
    if (prevVncConnectedRef.current && !isVncConnected) {
      setVncDisconnectedTrigger((x) => x + 1);
    }
    prevVncConnectedRef.current = isVncConnected;
  }, [isVncConnected]);

  useEffect(
    () => {
      if (!showStream || !canvasContainer || !workflowRunId) {
        if (rfbRef.current) {
          rfbRef.current.disconnect();
          rfbRef.current = null;
          setIsVncConnected(false);
        }
        return;
      }

      async function setupVnc() {
        let credentialQueryParam = "";

        if (environment === "local") {
          credentialQueryParam = `?apikey=${envCredential}`;
        } else {
          if (credentialGetter) {
            const token = await credentialGetter();
            credentialQueryParam = `?token=Bearer ${token}`;
          } else {
            credentialQueryParam = `?apikey=${envCredential}`;
          }
        }

        if (rfbRef.current && isVncConnected) {
          return;
        }

        const vncUrl = `${wssBaseUrl}/stream/vnc/workflow_run/${workflowRunId}${credentialQueryParam}`;

        if (rfbRef.current) {
          rfbRef.current.disconnect();
        }

        const canvas = canvasContainer;

        if (!canvas) {
          throw new Error("Canvas element not found");
        }

        const rfb = new RFB(canvas, vncUrl);

        rfb.scaleViewport = true;

        rfbRef.current = rfb;

        rfb.addEventListener("connect", () => {
          setIsVncConnected(true);
        });

        rfb.addEventListener("disconnect", async (/* e: RfbEvent */) => {
          setIsVncConnected(false);

          queryClient.invalidateQueries({
            queryKey: ["workflowRun", workflowPermanentId, workflowRunId],
          });
          queryClient.invalidateQueries({ queryKey: ["workflowRuns"] });
          queryClient.invalidateQueries({
            queryKey: ["workflowTasks", workflowRunId],
          });
          queryClient.invalidateQueries({ queryKey: ["runs"] });
        });
      }

      setupVnc();

      return () => {
        if (rfbRef.current) {
          rfbRef.current.disconnect();
          rfbRef.current = null;
        }
        setIsVncConnected(false);
      };
    },
    // cannot include isVncConnected in deps as it will cause infinite loop
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      credentialGetter,
      workflowRunId,
      workflowPermanentId,
      showStream,
      queryClient,
      canvasContainer,
      vncDisconnectedTrigger, // will re-run on disconnects
    ],
  );

  // Effect to show toast when workflow reaches a final state based on hook updates
  useEffect(() => {
    if (workflowRun) {
      if (
        workflowRun.status === Status.Failed ||
        workflowRun.status === Status.Terminated
      ) {
        // Only show toast if VNC is not connected or was never connected,
        // to avoid double toasting if disconnect handler also triggers similar logic.
        // However, the disconnect handler now primarily invalidates queries.
        toast({
          title: "Run Ended",
          description: `The workflow run has ${workflowRun.status}.`,
          variant: "destructive",
        });
      } else if (workflowRun.status === Status.Completed) {
        toast({
          title: "Run Completed",
          description: "The workflow run has been completed.",
          variant: "success",
        });
      }
    }
  }, [workflowRun, workflowRun?.status]);

  return (
    <div
      className={cn("workflow-run-stream-vnc", {
        "user-is-controlling": userIsControlling,
      })}
      ref={setCanvasContainerRef}
    >
      {isVncConnected && (
        <div className="overlay-container">
          <div className="overlay">
            <Button
              // className="take-control"
              className={cn("take-control", { hide: userIsControlling })}
              type="button"
              onClick={() => setUserIsControlling(true)}
            >
              <HandIcon className="mr-2 h-4 w-4" />
              take control
            </Button>
            <div className="absolute bottom-[-1rem] right-[1rem]">
              <Button
                className={cn("relinquish-control", {
                  hide: !userIsControlling,
                })}
                type="button"
                onClick={() => setUserIsControlling(false)}
              >
                <PlayIcon className="mr-2 h-4 w-4" />
                run agent
              </Button>
            </div>
          </div>
        </div>
      )}
      {!isVncConnected && (
        <div className="absolute left-0 top-0 flex h-full w-full items-center justify-center bg-black">
          <Skeleton className="aspect-[16/9] h-auto max-h-full w-full max-w-full rounded-lg object-cover" />
        </div>
      )}
    </div>
  );
}

export { WorkflowRunStreamVnc };
