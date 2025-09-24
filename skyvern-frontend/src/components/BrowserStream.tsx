import RFB from "@novnc/novnc/lib/rfb.js";
import { ExitIcon, HandIcon } from "@radix-ui/react-icons";
import { useEffect, useState, useRef, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { Status } from "@/api/types";
import type {
  TaskApiResponse,
  WorkflowRunStatusApiResponse,
} from "@/api/types";
import { Button } from "@/components/ui/button";
import { AnimatedWave } from "@/components/AnimatedWave";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import { useClientIdStore } from "@/store/useClientIdStore";
import {
  envCredential,
  environment,
  wssBaseUrl,
  newWssBaseUrl,
} from "@/util/env";
import { cn } from "@/util/utils";

import { RotateThrough } from "./RotateThrough";
import "./browser-stream.css";

interface BrowserSession {
  browser_session_id: string;
  completed_at?: string;
}

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
  showControlButtons?: boolean;
  task?: {
    run: TaskApiResponse;
  };
  workflow?: {
    run: WorkflowRunStatusApiResponse;
  };
  resizeTrigger?: number;
  // --
  onClose?: () => void;
};

function BrowserStream({
  browserSessionId = undefined,
  interactive = true,
  showControlButtons = undefined,
  task = undefined,
  workflow = undefined,
  resizeTrigger,
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
    browserSessionId = workflow.run.browser_session_id ?? undefined;
    showStream = statusIsNotFinalized(workflow.run);
    entity = "workflow";
  } else {
    throw new Error("No browser session id, task or workflow provided");
  }

  useQuery({
    queryKey: ["browserSession", browserSessionId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");

      try {
        const response = await client.get<BrowserSession | null>(
          `/browser_sessions/${browserSessionId}`,
        );
        const browserSession = response.data;

        if (!browserSession || browserSession.completed_at) {
          setHasBrowserSession(false);
          return false;
        }

        setHasBrowserSession(true);
        return true;
      } catch (error) {
        setHasBrowserSession(false);
        return false;
      }
    },
    enabled: !!browserSessionId,
    refetchInterval: 5000,
  });

  const [hasBrowserSession, setHasBrowserSession] = useState(true); // be optimistic
  const [userIsControlling, setUserIsControlling] = useState(interactive);
  const [commandSocket, setCommandSocket] = useState<WebSocket | null>(null);
  const [vncDisconnectedTrigger, setVncDisconnectedTrigger] = useState(0);
  const prevVncConnectedRef = useRef<boolean>(false);
  const [isVncConnected, setIsVncConnected] = useState<boolean>(false);
  const [commandDisconnectedTrigger, setCommandDisconnectedTrigger] =
    useState(0);
  const prevCommandConnectedRef = useRef<boolean>(false);
  const [isCommandConnected, setIsCommandConnected] = useState<boolean>(false);
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

        if (!hasBrowserSession) {
          setIsVncConnected(false);
          return;
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

        // setIsVncConnected(true); // be optimistic
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
      browserSessionId,
      entity,
      canvasContainer,
      hasBrowserSession,
      runId,
      showStream,
      vncDisconnectedTrigger, // will re-run on disconnects
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

      if (!hasBrowserSession) {
        setIsCommandConnected(false);
        return;
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
    browserSessionId,
    canvasContainer,
    commandDisconnectedTrigger,
    entity,
    getWebSocketParams,
    hasBrowserSession,
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

    if (interactive || userIsControlling) {
      sendCommand({ kind: "take-control" });
    } else {
      sendCommand({ kind: "cede-control" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interactive, isCommandConnected, userIsControlling]);

  // Effect to handle window resize trigger for NoVNC canvas
  useEffect(() => {
    if (!resizeTrigger || !canvasContainer || !rfbRef.current) {
      return;
    }

    // const originalDisplay = canvasContainer.style.display;
    // canvasContainer.style.display = "none";
    // canvasContainer.offsetHeight;
    // canvasContainer.style.display = originalDisplay;
    // window.dispatchEvent(new Event("resize"));
  }, [resizeTrigger, canvasContainer]);

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

  const theUserIsControlling =
    userIsControlling || (interactive && !showControlButtons);

  return (
    <div
      className={cn(
        "browser-stream relative flex items-center justify-center",
        {
          "user-is-controlling": theUserIsControlling,
        },
      )}
      ref={setCanvasContainerRef}
    >
      {isVncConnected && hasBrowserSession && (
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
      {!isVncConnected && (
        <div className="absolute left-0 top-1/2 flex aspect-video max-h-full w-full -translate-y-1/2 flex-col items-center justify-center gap-2 rounded-md border border-slate-800 text-sm text-slate-400">
          {browserSessionId && !hasBrowserSession ? (
            <div>This live browser session is no longer streaming.</div>
          ) : (
            <>
              <RotateThrough interval={7 * 1000}>
                <span>Hm, working on the connection...</span>
                <span>Hang tight, we're almost there...</span>
                <span>Just a moment...</span>
                <span>Backpropagating...</span>
                <span>Attention is all I need...</span>
                <span>Consulting the manual...</span>
                <span>Looking for the bat phone...</span>
                <span>Where's Shu?...</span>
              </RotateThrough>
              <AnimatedWave text=".‧₊˚ ⋅ ? ✨ ?★ ‧₊˚ ⋅" />
            </>
          )}
        </div>
      )}
    </div>
  );
}

export { BrowserStream };
