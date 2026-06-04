import { getClient } from "@/api/AxiosClient";
import { Status, StepApiResponse, TaskApiResponse } from "@/api/types";
import { BrowserStream } from "@/components/BrowserStream";
import { AspectRatio } from "@/components/ui/aspect-ratio";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/use-toast";
import { ZoomableImage } from "@/components/ZoomableImage";
import { useCostCalculator } from "@/hooks/useCostCalculator";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useBrowserStreamingMode } from "@/hooks/useRuntimeConfig";
import { getCredentialParam } from "@/util/env";
import {
  StreamStatusPanel,
  type StreamDiagnostic,
} from "@/routes/streaming/StreamDiagnostics";
import {
  keepPreviousData,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import {
  statusIsFinalized,
  statusIsNotFinalized,
  statusIsRunningOrQueued,
} from "../types";
import { ActionScreenshot } from "./ActionScreenshot";
import { useActions } from "./hooks/useActions";
import { ScrollableActionList } from "./ScrollableActionList";
import { useFirstParam } from "@/hooks/useFirstParam";

const formatter = Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});

type StreamMessage = {
  task_id: string;
  status: string;
  screenshot?: string;
  format?: string;
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
        title: "We've misplaced this task",
        detail: "The backend can't find it for your org.",
      };
    case "timeout":
      return {
        title: "The browser's gone strangely quiet",
        detail: "The task started, but no active page showed up to stream.",
        hint: "Check backend logs for browser launch errors or a streaming-mode mismatch.",
      };
    case "completed":
    case "failed":
    case "terminated":
      return {
        title: "This task has wrapped up",
        detail: `It's no longer live — status: ${status}.`,
      };
    default:
      return {
        title: "Waiting for browser frames",
        detail: `The stream is connected and the task status is ${status}.`,
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

const wssBaseUrl = import.meta.env.VITE_WSS_BASE_URL;

function TaskActions() {
  const taskId = useFirstParam("taskId", "runId");
  const credentialGetter = useCredentialGetter();
  const [streamImgSrc, setStreamImgSrc] = useState<string>("");
  const [streamFormat, setStreamFormat] = useState<string>("png");
  const [streamDiagnostic, setStreamDiagnostic] =
    useState<StreamDiagnostic>(STARTING_DIAGNOSTIC);
  const socketRef = useRef<WebSocket | null>(null);
  const hasFrameRef = useRef(false);
  const [selectedAction, setSelectedAction] = useState<
    number | "stream" | null
  >(null);
  const costCalculator = useCostCalculator();
  const queryClient = useQueryClient();

  const { data: task, isLoading: taskIsLoading } = useQuery<TaskApiResponse>({
    queryKey: ["task", taskId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client.get(`/tasks/${taskId}`).then((response) => response.data);
    },
    refetchInterval: (query) => {
      if (!query.state.data) {
        return false;
      }
      if (statusIsNotFinalized(query.state.data)) {
        return 5000;
      }
      return false;
    },
    placeholderData: keepPreviousData,
  });
  const taskIsNotFinalized = task && statusIsNotFinalized(task);
  const taskIsRunningOrQueued = task && statusIsRunningOrQueued(task);
  const browserSessionId = task?.browser_session_id;
  const { browserStreamingMode } = useBrowserStreamingMode();
  const shouldUseCdpStream = browserStreamingMode === "cdp";

  useEffect(() => {
    // In VNC mode, BrowserStream handles live sessions. In CDP mode, this
    // screenshot WebSocket is the live stream.
    if (browserSessionId && !shouldUseCdpStream) {
      return;
    }

    if (!taskIsRunningOrQueued) {
      return;
    }
    setStreamDiagnostic(STARTING_DIAGNOSTIC);
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
        `${wssBaseUrl}/stream/tasks/${taskId}?${credentialParam}`,
      );

      socketRef.current.addEventListener("open", () => {
        setStreamDiagnostic({
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
          if (!message.screenshot && message.status) {
            setStreamDiagnostic(diagnosticForStatus(message.status));
          }
          if (
            message.status === "completed" ||
            message.status === "failed" ||
            message.status === "terminated"
          ) {
            socketRef.current?.close();
            queryClient.invalidateQueries({
              queryKey: ["tasks"],
            });
            if (
              message.status === "failed" ||
              message.status === "terminated"
            ) {
              toast({
                title: "Task Failed",
                description: "The task has failed.",
                variant: "destructive",
              });
            } else if (message.status === "completed") {
              toast({
                title: "Task Completed",
                description: "The task has been completed.",
                variant: "success",
              });
            }
          }
        } catch (e) {
          console.error("Failed to parse message", e);
          setStreamDiagnostic({
            title: "The stream said something funny",
            detail: "The browser sent a message the UI couldn't parse.",
          });
        }
      });

      socketRef.current.addEventListener("error", () => {
        setStreamDiagnostic({
          title: "The stream hit a snag",
          detail: "The connection ran into a network or server error.",
        });
      });

      socketRef.current.addEventListener("close", (event) => {
        if (!cancelled && !hasFrameRef.current) {
          setStreamDiagnostic(diagnosticForClose(event));
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
    browserSessionId,
    credentialGetter,
    taskId,
    taskIsRunningOrQueued,
    queryClient,
    shouldUseCdpStream,
  ]);

  const { data: steps, isLoading: stepsIsLoading } = useQuery<
    Array<StepApiResponse>
  >({
    queryKey: ["task", taskId, "steps"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`/tasks/${taskId}/steps`)
        .then((response) => response.data);
    },
    enabled: !!task,
    refetchOnWindowFocus: taskIsNotFinalized,
    refetchInterval: taskIsNotFinalized ? 5000 : false,
    placeholderData: keepPreviousData,
  });

  const { data: actions, isLoading: actionsIsLoading } = useActions({
    id: taskId ?? undefined,
  });

  if (taskIsLoading || actionsIsLoading || stepsIsLoading) {
    return (
      <div className="flex gap-2">
        <div className="h-[40rem] w-3/4">
          <Skeleton className="h-full" />
        </div>
        <div className="h-[40rem] w-1/4">
          <Skeleton className="h-full" />
        </div>
      </div>
    );
  }

  function getActiveSelection() {
    if (selectedAction === null) {
      if (taskIsNotFinalized) {
        return "stream";
      }
      return actions.length - 1;
    }
    if (selectedAction === "stream" && task && statusIsFinalized(task)) {
      return actions.length - 1;
    }
    return selectedAction;
  }

  const activeSelection = getActiveSelection();

  const activeAction =
    activeSelection !== "stream" ? actions[activeSelection] : null;

  function getStream() {
    // Use VNC streaming via BrowserStream when browser_session_id is available
    // and local browser streaming is not enabled.
    if (browserSessionId && !shouldUseCdpStream) {
      return (
        <AspectRatio ratio={16 / 9}>
          <BrowserStream
            key={browserSessionId}
            browserSessionId={browserSessionId}
            interactive={false}
            showControlButtons={false}
            onClose={() => {
              queryClient.invalidateQueries({
                queryKey: ["task", taskId],
              });
              queryClient.invalidateQueries({
                queryKey: ["tasks"],
              });
            }}
          />
        </AspectRatio>
      );
    }

    // Fall back to screenshot-based streaming
    if (task?.status === Status.Created) {
      return (
        <div className="flex h-full w-full flex-col items-center justify-center gap-4 bg-slate-elevation1 text-lg">
          <span>Task has been created.</span>
          <span>Stream will start when the task is running.</span>
        </div>
      );
    }
    if (task?.status === Status.Queued) {
      return (
        <div className="flex h-full w-full flex-col items-center justify-center gap-4 bg-slate-elevation1 text-lg">
          <span>Your task is queued. Typical queue time is 1-2 minutes.</span>
          <span>Stream will start when the task is running.</span>
        </div>
      );
    }

    if (task?.status === Status.Running && streamImgSrc.length === 0) {
      return <StreamStatusPanel diagnostic={streamDiagnostic} />;
    }

    if (task?.status === Status.Running && streamImgSrc.length > 0) {
      return (
        <div className="h-full w-full">
          <ZoomableImage
            src={`data:image/${streamFormat};base64,${streamImgSrc}`}
          />
        </div>
      );
    }
    return null;
  }

  const showCost = typeof costCalculator === "function";
  const notRunningSteps = steps?.filter((step) => step.status !== "running");

  return (
    <div className="flex gap-2">
      <div className="w-2/3 rounded border">
        <div className="h-full w-full p-4">
          {activeSelection === "stream" ? getStream() : null}
          {typeof activeSelection === "number" && activeAction ? (
            <ActionScreenshot
              artifactId={activeAction.screenshotArtifactId ?? undefined}
              stepId={activeAction.stepId}
              index={activeAction.index}
              taskStatus={task?.status}
            />
          ) : null}
        </div>
      </div>
      <ScrollableActionList
        activeIndex={activeSelection}
        data={actions ?? []}
        onActiveIndexChange={setSelectedAction}
        showStreamOption={Boolean(taskIsNotFinalized)}
        taskDetails={{
          steps: steps?.length ?? 0,
          actions: actions?.length ?? 0,
          cost: showCost
            ? formatter.format(costCalculator(notRunningSteps ?? []))
            : undefined,
        }}
      />
    </div>
  );
}

export { TaskActions };
