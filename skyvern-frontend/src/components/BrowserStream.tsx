import { Status } from "@/api/types";
import { useEffect, useState, useRef, useCallback } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { envCredential } from "@/util/env";
import { toast } from "@/components/ui/use-toast";
import RFB from "@novnc/novnc/lib/rfb.js";
import { environment, wssBaseUrl, newWssBaseUrl } from "@/util/env";
import { cn } from "@/util/utils";
import { useClientIdStore } from "@/store/useClientIdStore";
import type {
  TaskApiResponse,
  WorkflowRunStatusApiResponse,
} from "@/api/types";
import "./browser-stream.css";

interface CommandTakeControl {
  kind: "take-control";
}

interface CommandCedeControl {
  kind: "cede-control";
}

type Command = CommandTakeControl | CommandCedeControl;

type Props = {
  browserSessionId?: string;
  interactive?: boolean;
  task?: {
    run: TaskApiResponse;
  };
  workflow?: {
    run: WorkflowRunStatusApiResponse;
  };
  // --
  onClose?: () => void;
};

function BrowserStream({
  browserSessionId = undefined,
  interactive = true,
  task = undefined,
  workflow = undefined,
  // --
  onClose,
}: Props) {
  let showStream: boolean = false;
  let runId: string;
  let entity: "browserSession" | "task" | "workflow";

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
    showStream = statusIsNotFinalized(workflow.run);
    entity = "workflow";
  } else {
    throw new Error("No browser session, task or workflow provided");
  }

  const [commandSocket, setCommandSocket] = useState<WebSocket | null>(null);
  const [vncDisconnectedTrigger, setVncDisconnectedTrigger] = useState(0);
  const prevVncConnectedRef = useRef<boolean>(false);
  const [isVncConnected, setIsVncConnected] = useState<boolean>(false);
  const [commandDisconnectedTrigger, setCommandDisconnectedTrigger] =
    useState(0);
  const prevCommandConnectedRef = useRef<boolean>(false);
  const [isCommandConnected, setIsCommandConnected] = useState<boolean>(false);
  // goes up a level
  // const queryClient = useQueryClient();
  const [canvasContainer, setCanvasContainer] = useState<HTMLDivElement | null>(
    null,
  );
  const setCanvasContainerRef = useCallback((node: HTMLDivElement | null) => {
    setCanvasContainer(node);
  }, []);
  const rfbRef = useRef<RFB | null>(null);
  const clientId = useClientIdStore((state) => state.clientId);
  const credentialGetter = useCredentialGetter();

  const getWebSocketParams = useCallback(async () => {
    const clientIdQueryParam = `client_id=${clientId}`;
    let credentialQueryParam = "";

    if (environment === "local") {
      credentialQueryParam = `apikey=${envCredential}`;
    } else {
      if (credentialGetter) {
        const token = await credentialGetter();
        credentialQueryParam = `token=Bearer ${token}`;
      } else {
        credentialQueryParam = `apikey=${envCredential}`;
      }
    }

    const params = [credentialQueryParam, clientIdQueryParam].join("&");

    return `${params}`;
  }, [clientId, credentialGetter]);

  // effect for vnc disconnects only
  useEffect(() => {
    if (prevVncConnectedRef.current && !isVncConnected) {
      setVncDisconnectedTrigger((x) => x + 1);
      onClose?.();
    }
    prevVncConnectedRef.current = isVncConnected;
  }, [isVncConnected, onClose]);

  // effect for command disconnects only
  useEffect(() => {
    if (prevCommandConnectedRef.current && !isCommandConnected) {
      setCommandDisconnectedTrigger((x) => x + 1);
      onClose?.();
    }
    prevCommandConnectedRef.current = isCommandConnected;
  }, [isCommandConnected, onClose]);

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

      async function setupVnc() {
        if (rfbRef.current && isVncConnected) {
          return;
        }

        const wsParams = await getWebSocketParams();
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
      canvasContainer,
      showStream,
      vncDisconnectedTrigger, // will re-run on disconnects
      runId,
      entity,
    ],
  );

  // command socket
  useEffect(() => {
    if (!showStream || !canvasContainer || !runId) {
      return;
    }

    let ws: WebSocket | null = null;

    const connect = async () => {
      const wsParams = await getWebSocketParams();

      const commandUrl =
        entity === "browserSession"
          ? `${newWssBaseUrl}/stream/commands/browser_session/${runId}?${wsParams}`
          : entity === "task"
            ? `${wssBaseUrl}/stream/commands/task/${runId}?${wsParams}`
            : entity === "workflow"
              ? `${wssBaseUrl}/stream/commands/workflow_run/${runId}?${wsParams}`
              : null;

      if (!commandUrl) {
        throw new Error("No command url");
      }

      ws = new WebSocket(commandUrl);

      ws.onopen = () => {
        setIsCommandConnected(true);
        setCommandSocket(ws);
      };

      ws.onclose = () => {
        setIsCommandConnected(false);
        setCommandSocket(null);
      };
    };

    connect();

    return () => {
      try {
        ws && ws.close();
      } catch (e) {
        // pass
      }
    };
  }, [
    canvasContainer,
    commandDisconnectedTrigger,
    entity,
    getWebSocketParams,
    runId,
    showStream,
  ]);

  // effect to send a command when the user is controlling, vs not controlling
  useEffect(() => {
    if (!isCommandConnected) {
      return;
    }

    const sendCommand = (command: Command) => {
      if (!commandSocket) {
        console.warn("Cannot send command, as command socket is closed.");
        console.warn(command);
        return;
      }

      commandSocket.send(JSON.stringify(command));
    };

    if (interactive) {
      sendCommand({ kind: "take-control" });
    } else {
      sendCommand({ kind: "cede-control" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interactive, isCommandConnected]);

  // Effect to show toast when task or workflow reaches a final state based on hook updates
  useEffect(() => {
    const run = task ? task.run : workflow ? workflow.run : null;

    if (!run) {
      return;
    }

    const name = task ? "task" : workflow ? "workflow" : null;

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

  return (
    <div
      className={cn("browser-stream", {
        "user-is-controlling": interactive,
      })}
      ref={setCanvasContainerRef}
    >
      {isVncConnected && <div className="overlay" />}
      {!isVncConnected && (
        <div className="absolute left-0 top-0 flex h-full w-full items-center justify-center bg-black">
          <Skeleton className="aspect-[16/9] h-auto max-h-full w-full max-w-full rounded-lg object-cover" />
        </div>
      )}
    </div>
  );
}

export { BrowserStream };
