import { getClient } from "@/api/AxiosClient";
import { Status, StepApiResponse, TaskApiResponse } from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/use-toast";
import { ZoomableImage } from "@/components/ZoomableImage";
import { useCostCalculator } from "@/hooks/useCostCalculator";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getRuntimeApiKey } from "@/util/env";
import {
  keepPreviousData,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useState } from "react";
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
};

let socket: WebSocket | null = null;

const wssBaseUrl = import.meta.env.VITE_WSS_BASE_URL;

function TaskActions() {
  const taskId = useFirstParam("taskId", "runId");
  const credentialGetter = useCredentialGetter();
  const [streamImgSrc, setStreamImgSrc] = useState<string>("");
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

  useEffect(() => {
    if (!taskIsRunningOrQueued) {
      return;
    }

    async function run() {
      // Create WebSocket connection.
      let credential = null;
      if (credentialGetter) {
        const token = await credentialGetter();
        credential = `?token=Bearer ${token}`;
      } else {
        const apiKey = getRuntimeApiKey();
        credential = apiKey ? `?apikey=${apiKey}` : "";
      }
      if (socket) {
        socket.close();
      }
      socket = new WebSocket(
        `${wssBaseUrl}/stream/tasks/${taskId}${credential}`,
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
  }, [credentialGetter, taskId, taskIsRunningOrQueued, queryClient]);

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
      return (
        <div className="flex h-full w-full items-center justify-center bg-slate-elevation1 text-lg">
          Starting the stream...
        </div>
      );
    }

    if (task?.status === Status.Running && streamImgSrc.length > 0) {
      return (
        <div className="h-full w-full">
          <ZoomableImage src={`data:image/png;base64,${streamImgSrc}`} />
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
