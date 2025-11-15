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
  environment,
  wssBaseUrl,
  newWssBaseUrl,
  getRuntimeApiKey,
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

// a "Command" is an fire-n-forget out-message - it does not require a response
type Command = CommandTakeControl | CommandCedeControl;

const messageInKinds = ["ask-for-clipboard", "copied-text"] as const;

type MessageInKind = (typeof messageInKinds)[number];

interface MessageInAskForClipboard {
  kind: "ask-for-clipboard";
}

interface MessageInCopiedText {
  kind: "copied-text";
  text: string;
}

type MessageIn = MessageInCopiedText | MessageInAskForClipboard;

interface MessageOutAskForClipboardResponse {
  kind: "ask-for-clipboard-response";
  text: string;
}

type MessageOut = MessageOutAskForClipboardResponse;

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
  isVisible?: boolean;
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
  isVisible = true,
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
          return false;

          // NOTE(jdo:streaming-local-dev): remove above and use this instead
          // if (browserSession && browserSession.completed_at) {
          //   console.warn(
          //     "Completed at:",
          //     browserSession.completed_at,
          //     "continuing anyway!",
          //   );
          //   setHasBrowserSession(true);
          //   return true;
          // } else {
          //   setHasBrowserSession(false);
          //   return false;
          // }
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
  const [userIsControlling, setUserIsControlling] = useState(false);
  const [messageSocket, setMessageSocket] = useState<WebSocket | null>(null);
  const [vncDisconnectedTrigger, setVncDisconnectedTrigger] = useState(0);
  const prevVncConnectedRef = useRef<boolean>(false);
  const [isVncConnected, setIsVncConnected] = useState<boolean>(false);
  const [isCanvasReady, setIsCanvasReady] = useState<boolean>(false);
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
  const observerRef = useRef<MutationObserver | null>(null);
  const clientId = useClientIdStore((state) => state.clientId);
  const credentialGetter = useCredentialGetter();

  const getWebSocketParams = useCallback(async () => {
    const clientIdQueryParam = `client_id=${clientId}`;
    const runtimeApiKey = getRuntimeApiKey();

    let credentialQueryParam = runtimeApiKey ? `apikey=${runtimeApiKey}` : "";

    if (environment !== "local" && credentialGetter) {
      const token = await credentialGetter();
      credentialQueryParam = token ? `token=Bearer ${token}` : "";
    }

    return credentialQueryParam
      ? `${credentialQueryParam}&${clientIdQueryParam}`
      : clientIdQueryParam;
  }, [clientId, credentialGetter]);

  // browser is ready
  useEffect(() => {
    setIsReady(isVncConnected && isCanvasReady && hasBrowserSession);
  }, [hasBrowserSession, isCanvasReady, isVncConnected]);

  // effect for vnc disconnects only
  useEffect(() => {
    if (prevVncConnectedRef.current && !isVncConnected) {
      setVncDisconnectedTrigger((x) => x + 1);
      onClose?.();
    }
    prevVncConnectedRef.current = isVncConnected;
  }, [isVncConnected, onClose]);

  // effect for message disconnects only
  useEffect(() => {
    if (prevMessageConnectedRef.current && !isMessageConnected) {
      setMessagesDisconnectedTrigger((x) => x + 1);
      onClose?.();
    }
    prevMessageConnectedRef.current = isMessageConnected;
  }, [isMessageConnected, onClose]);

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

        rfbRef.current = rfb;

        const canvasElement = canvasContainer.querySelector("canvas");

        if (canvasElement) {
          setIsCanvasReady(true);
          observerRef.current?.disconnect();
        }

        rfb.addEventListener("connect", () => {
          setIsVncConnected(true);
        });

        rfb.addEventListener("disconnect", async (/* e: RfbEvent */) => {
          setIsVncConnected(false);
          setIsCanvasReady(false);
        });

        setIsVncConnected(true); // be optimistic
      }

      setupVnc();

      return () => {
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
      hasBrowserSession,
      runId,
      showStream,
      vncDisconnectedTrigger, // will re-run on disconnects
    ],
  );

  useEffect(() => {
    if (!showStream || !canvasContainer || !runId) {
      return;
    }

    let ws: WebSocket | null = null;

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

      if (!hasBrowserSession) {
        setIsMessageConnected(false);
        return;
      }

      ws = new WebSocket(messageUrl);

      ws.onopen = () => {
        setIsMessageConnected(true);
        setMessageSocket(ws);
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

      ws.onclose = () => {
        setIsMessageConnected(false);
        setMessageSocket(null);
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
    // NOTE: adding getWebSocketParams causes constant reconnects of message channel when,
    // for instance, take-control or cede-control is clicked
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    browserSessionId,
    canvasContainer,
    messagesDisconnectedTrigger,
    entity,
    hasBrowserSession,
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
        console.warn("Cannot send command, as command socket is closed.");
        console.warn(command);
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

  useEffect(() => {
    if (!interactive) {
      setUserIsControlling(false);
    }
  }, [interactive]);
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
      default: {
        const _exhaustive: never = kind;
        return _exhaustive;
      }
    }
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
                "Pasting your current clipboard text into the web page. NOTE: copy-paste only works in the web page - not in the browser (like the address bar).",
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

        navigator.clipboard
          .writeText(text)
          .then(() => {
            toast({
              title: "Copied to Clipboard",
              description:
                "The text has been copied to your clipboard. NOTE: copy-paste only works in the web page - not in the browser (like the address bar).",
            });
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
      default: {
        const _exhaustive: never = kind;
        return _exhaustive;
      }
    }
  };

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
      {!isReady && (
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
