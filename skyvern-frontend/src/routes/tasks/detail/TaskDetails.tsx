import { getClient } from "@/api/AxiosClient";
import {
  Status,
  TaskApiResponse,
  WorkflowRunStatusApiResponse,
} from "@/api/types";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { cn } from "@/util/utils";
import { CopyIcon, PlayIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, NavLink, Outlet, useParams } from "react-router-dom";
import { useTaskQuery } from "./hooks/useTaskQuery";
import { taskIsFinalized } from "@/api/utils";
import fetchToCurl from "fetch-to-curl";
import { apiBaseUrl } from "@/util/env";
import { useApiCredential } from "@/hooks/useApiCredential";
import { copyText } from "@/util/copyText";
import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";
import { StatusBadge } from "@/components/StatusBadge";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";

function createTaskRequestObject(values: TaskApiResponse) {
  return {
    url: values.request.url,
    webhook_callback_url: values.request.webhook_callback_url,
    navigation_goal: values.request.navigation_goal,
    data_extraction_goal: values.request.data_extraction_goal,
    proxy_location: values.request.proxy_location,
    error_code_mapping: values.request.error_code_mapping,
    navigation_payload: values.request.navigation_payload,
    extracted_information_schema: values.request.extracted_information_schema,
  };
}

function TaskDetails() {
  const { taskId } = useParams();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const apiCredential = useApiCredential();

  const {
    data: task,
    isLoading: taskIsLoading,
    isError: taskIsError,
    error: taskError,
  } = useTaskQuery({ id: taskId });

  const { data: workflowRun, isLoading: workflowRunIsLoading } =
    useQuery<WorkflowRunStatusApiResponse>({
      queryKey: ["workflowRun", task?.workflow_run_id],
      queryFn: async () => {
        const client = await getClient(credentialGetter);
        return client
          .get(`/workflows/runs/${task?.workflow_run_id}`)
          .then((response) => response.data);
      },
      enabled: !!task?.workflow_run_id,
    });

  const { data: workflow, isLoading: workflowIsLoading } =
    useQuery<WorkflowApiResponse>({
      queryKey: ["workflow", workflowRun?.workflow_id],
      queryFn: async () => {
        const client = await getClient(credentialGetter);
        return client
          .get(`/workflows/${workflowRun?.workflow_id}`)
          .then((response) => response.data);
      },
      enabled: !!workflowRun?.workflow_id,
    });

  const cancelTaskMutation = useMutation({
    mutationFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .post(`/tasks/${taskId}/cancel`)
        .then((response) => response.data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["task", taskId],
      });
      queryClient.invalidateQueries({
        queryKey: ["tasks"],
      });
      toast({
        variant: "success",
        title: "Task Canceled",
        description: "The task has been successfully canceled.",
      });
    },
    onError: (error) => {
      toast({
        variant: "destructive",
        title: "Error",
        description: error.message,
      });
    },
  });

  if (taskIsError) {
    return <div>Error: {taskError?.message}</div>;
  }

  const showExtractedInformation =
    task?.status === Status.Completed && task.extracted_information !== null;
  const extractedInformation = showExtractedInformation ? (
    <div className="space-y-1">
      <Label className="text-lg">Extracted Information</Label>
      <CodeEditor
        language="json"
        value={JSON.stringify(task.extracted_information, null, 2)}
        readOnly
        minHeight={"96px"}
        maxHeight={"500px"}
        className="w-full"
      />
    </div>
  ) : null;

  const taskIsRunningOrQueued =
    task?.status === Status.Running || task?.status === Status.Queued;

  const taskHasTerminalState = task && taskIsFinalized(task);

  const showFailureReason =
    task?.status === Status.Failed ||
    task?.status === Status.Terminated ||
    task?.status === Status.TimedOut;
  const failureReason = showFailureReason ? (
    <div className="space-y-1">
      <Label className="text-lg">Failure Reason</Label>
      <CodeEditor
        language="json"
        value={JSON.stringify(task.failure_reason, null, 2)}
        readOnly
        minHeight={"96px"}
        maxHeight={"500px"}
        className="w-full"
      />
    </div>
  ) : null;

  return (
    <div className="flex flex-col gap-8">
      <header className="space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-5">
            <span className="text-3xl">{taskId}</span>
            {taskIsLoading ? (
              <Skeleton className="h-8 w-32" />
            ) : (
              task && <StatusBadge status={task.status} />
            )}
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="secondary"
              onClick={() => {
                if (!task) {
                  return;
                }
                const curl = fetchToCurl({
                  method: "POST",
                  url: `${apiBaseUrl}/tasks`,
                  body: createTaskRequestObject(task),
                  headers: {
                    "Content-Type": "application/json",
                    "x-api-key": apiCredential ?? "<your-api-key>",
                  },
                });
                copyText(curl).then(() => {
                  toast({
                    variant: "success",
                    title: "Copied to Clipboard",
                    description:
                      "The cURL command has been copied to your clipboard.",
                  });
                });
              }}
            >
              <CopyIcon className="mr-2 h-4 w-4" />
              cURL
            </Button>
            {taskIsRunningOrQueued && (
              <Dialog>
                <DialogTrigger asChild>
                  <Button variant="destructive">Cancel</Button>
                </DialogTrigger>
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>Are you sure?</DialogTitle>
                    <DialogDescription>
                      Are you sure you want to cancel this task?
                    </DialogDescription>
                  </DialogHeader>
                  <DialogFooter>
                    <DialogClose asChild>
                      <Button variant="secondary">Back</Button>
                    </DialogClose>
                    <Button
                      variant="destructive"
                      onClick={() => {
                        cancelTaskMutation.mutate();
                      }}
                      disabled={cancelTaskMutation.isPending}
                    >
                      {cancelTaskMutation.isPending && (
                        <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
                      )}
                      Cancel Task
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            )}
            {taskHasTerminalState && (
              <Button asChild>
                <Link to={`/create/retry/${task.task_id}`}>
                  <PlayIcon className="mr-2 h-4 w-4" />
                  Rerun
                </Link>
              </Button>
            )}
          </div>
        </div>
        <div className="text-2xl text-slate-400 underline underline-offset-4">
          {workflowIsLoading || workflowRunIsLoading ? (
            <Skeleton className="h-8 w-64" />
          ) : (
            workflow &&
            workflowRun && (
              <Link
                to={`/workflows/${workflow.workflow_permanent_id}/${workflowRun.workflow_run_id}`}
              >
                {workflow.title}
              </Link>
            )
          )}
        </div>
      </header>

      {taskIsLoading ? (
        <Skeleton className="h-32 w-full" />
      ) : (
        <>
          {extractedInformation}
          {failureReason}
        </>
      )}
      <div className="flex items-center justify-center">
        <div className="inline-flex rounded border bg-muted p-1">
          <NavLink
            to="actions"
            className={({ isActive }) => {
              return cn(
                "cursor-pointer rounded-md px-2 py-1 text-muted-foreground",
                {
                  "bg-primary-foreground text-foreground": isActive,
                },
              );
            }}
          >
            Actions
          </NavLink>
          <NavLink
            to="recording"
            className={({ isActive }) => {
              return cn(
                "cursor-pointer rounded-md px-2 py-1 text-muted-foreground",
                {
                  "bg-primary-foreground text-foreground": isActive,
                },
              );
            }}
          >
            Recording
          </NavLink>
          <NavLink
            to="parameters"
            className={({ isActive }) => {
              return cn(
                "cursor-pointer rounded-md px-2 py-1 text-muted-foreground",
                {
                  "bg-primary-foreground text-foreground": isActive,
                },
              );
            }}
          >
            Parameters
          </NavLink>
          <NavLink
            to="diagnostics"
            className={({ isActive }) => {
              return cn(
                "cursor-pointer rounded-md px-2 py-1 text-muted-foreground",
                {
                  "bg-primary-foreground text-foreground": isActive,
                },
              );
            }}
          >
            Diagnostics
          </NavLink>
        </div>
      </div>
      <Outlet />
    </div>
  );
}

export { TaskDetails };
