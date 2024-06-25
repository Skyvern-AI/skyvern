import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { ActionScreenshot } from "./ActionScreenshot";
import { ScrollableActionList } from "./ScrollableActionList";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  ActionApiResponse,
  ActionTypes,
  Status,
  StepApiResponse,
  TaskApiResponse,
} from "@/api/types";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/use-toast";
import { envCredential } from "@/util/env";

type StreamMessage = {
  task_id: string;
  status: string;
  screenshot?: string;
};

let socket: WebSocket | null = null;

const wssBaseUrl = import.meta.env.VITE_WSS_BASE_URL;

function getActionInput(action: ActionApiResponse) {
  let input = "";
  if (action.action_type === ActionTypes.InputText && action.text) {
    input = action.text;
  } else if (action.action_type === ActionTypes.Click) {
    input = "Click";
  } else if (action.action_type === ActionTypes.SelectOption && action.option) {
    input = action.option.label;
  }
  return input;
}

function TaskActions() {
  const { taskId } = useParams();
  const credentialGetter = useCredentialGetter();
  const [streamImgSrc, setStreamImgSrc] = useState<string>("");
  const [selectedAction, setSelectedAction] = useState<number | "stream">(0);

  const { data: task, isLoading: taskIsLoading } = useQuery<TaskApiResponse>({
    queryKey: ["task", taskId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client.get(`/tasks/${taskId}`).then((response) => response.data);
    },
    refetchInterval: (query) => {
      if (
        query.state.data?.status === Status.Running ||
        query.state.data?.status === Status.Queued
      ) {
        return 5000;
      }
      return false;
    },
    placeholderData: keepPreviousData,
  });
  const taskIsRunningOrQueued =
    task?.status === Status.Running || task?.status === Status.Queued;

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
        credential = `?apikey=${envCredential}`;
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
            setSelectedAction(0);
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
  }, [credentialGetter, taskId, taskIsRunningOrQueued]);

  useEffect(() => {
    if (!taskIsLoading && taskIsRunningOrQueued) {
      setSelectedAction("stream");
    }
  }, [taskIsLoading, taskIsRunningOrQueued]);

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
    refetchOnWindowFocus: taskIsRunningOrQueued,
    refetchInterval: taskIsRunningOrQueued ? 5000 : false,
    placeholderData: keepPreviousData,
  });

  const actions = steps
    ?.map((step) => {
      const actionsAndResults = step.output?.actions_and_results ?? [];

      const actions = actionsAndResults.map((actionAndResult, index) => {
        const action = actionAndResult[0];
        const actionResult = actionAndResult[1];
        if (actionResult.length === 0) {
          return null;
        }
        return {
          reasoning: action.reasoning,
          confidence: action.confidence_float,
          input: getActionInput(action),
          type: action.action_type,
          success: actionResult?.[0]?.success ?? false,
          stepId: step.step_id,
          index,
        };
      });
      return actions;
    })
    .flat();

  if (taskIsLoading || stepsIsLoading) {
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

  const activeAction =
    typeof selectedAction === "number" &&
    actions?.[actions.length - selectedAction - 1];

  function getStream() {
    if (task?.status === Status.Queued) {
      return (
        <div className="w-full h-full flex flex-col gap-4 items-center justify-center text-lg bg-slate-900">
          <span>Your task is queued. Typical queue time is 1-2 minutes.</span>
          <span>Stream will start when the task is running.</span>
        </div>
      );
    }

    if (task?.status === Status.Running && streamImgSrc.length === 0) {
      return (
        <div className="w-full h-full flex items-center justify-center text-lg bg-slate-900">
          Starting the stream...
        </div>
      );
    }

    if (task?.status === Status.Running && streamImgSrc.length > 0) {
      return (
        <div className="w-full h-full">
          <img src={`data:image/png;base64,${streamImgSrc}`} />
        </div>
      );
    }
    return null;
  }

  return (
    <div className="flex gap-2">
      <div className="w-2/3 border rounded">
        <div className="p-4 w-full h-full">
          {selectedAction === "stream" ? getStream() : null}
          {typeof selectedAction === "number" && activeAction ? (
            <ActionScreenshot
              stepId={activeAction.stepId}
              index={activeAction.index}
            />
          ) : null}
        </div>
      </div>
      <ScrollableActionList
        activeIndex={selectedAction}
        data={actions ?? []}
        onActiveIndexChange={setSelectedAction}
        showStreamOption={taskIsRunningOrQueued}
        onNext={() => {
          if (!actions) {
            return;
          }
          setSelectedAction((prev) => {
            if (taskIsRunningOrQueued) {
              if (actions.length === 0) {
                return "stream";
              }
              if (prev === actions.length - 1) {
                return actions.length - 1;
              }
              if (prev === "stream") {
                return 0;
              }
              return prev + 1;
            }
            if (typeof prev === "number") {
              return prev === actions.length - 1 ? prev : prev + 1;
            }
            return 0;
          });
        }}
        onPrevious={() => {
          if (!actions) {
            return;
          }
          setSelectedAction((prev) => {
            if (taskIsRunningOrQueued) {
              if (actions.length === 0) {
                return "stream";
              }
              if (prev === 0) {
                return "stream";
              }
              if (prev === "stream") {
                return "stream";
              }
              return prev - 1;
            }
            if (typeof prev === "number") {
              return prev === 0 ? prev : prev - 1;
            }
            return 0;
          });
        }}
      />
    </div>
  );
}

export { TaskActions };
