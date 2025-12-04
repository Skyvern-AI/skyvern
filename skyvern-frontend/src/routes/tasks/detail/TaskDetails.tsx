import { AxiosError } from "axios";
import { getClient } from "@/api/AxiosClient";
import { useState } from "react";
import {
  RunEngine,
  Status,
  TaskApiResponse,
  WorkflowRunStatusApiResponse,
} from "@/api/types";
import { Status404 } from "@/components/Status404";
import { StatusBadge } from "@/components/StatusBadge";
import { SwitchBarNavigation } from "@/components/SwitchBarNavigation";
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
import { useApiCredential } from "@/hooks/useApiCredential";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";
import { runsApiBaseUrl } from "@/util/env";
import { ApiWebhookActionsMenu } from "@/components/ApiWebhookActionsMenu";
import { WebhookReplayDialog } from "@/components/WebhookReplayDialog";
import { type ApiCommandOptions } from "@/util/apiCommands";
import { buildTaskRunPayload } from "@/util/taskRunPayload";
import { PlayIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, Outlet } from "react-router-dom";
import { statusIsFinalized } from "../types";
import { MAX_STEPS_DEFAULT } from "../constants";
import { useTaskQuery } from "./hooks/useTaskQuery";
import { useFirstParam } from "@/hooks/useFirstParam";
import * as env from "@/util/env";

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
  const taskId = useFirstParam("taskId", "runId");
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const apiCredential = useApiCredential();

  const {
    data: task,
    isLoading: taskIsLoading,
    isError: taskIsError,
    error: taskError,
  } = useTaskQuery({ id: taskId ?? undefined });

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
      if (task?.workflow_run_id) {
        queryClient.invalidateQueries({
          queryKey: ["workflowRun", task.workflow_run_id],
        });
        queryClient.invalidateQueries({
          queryKey: [
            "workflowRun",
            workflow?.workflow_permanent_id,
            task.workflow_run_id,
          ],
        });
      }
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

  const [replayOpen, setReplayOpen] = useState(false);

  if (taskIsError) {
    const status = (taskError as AxiosError | undefined)?.response?.status;

    if (status === 404) {
      return <Status404 />;
    }

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

  const taskHasTerminalState = task && statusIsFinalized(task);

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

  const webhookFailureReason = task?.webhook_failure_reason ? (
    <div className="space-y-1">
      <Label>Webhook Failure Reason</Label>
      <div className="rounded-md border border-yellow-600 p-4 text-sm">
        {task.webhook_failure_reason}
      </div>
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
            {/** API & Webhooks consolidated dropdown + controlled dialog */}
            <ApiWebhookActionsMenu
              getOptions={() => {
                if (!task) {
                  return {
                    method: "GET",
                    url: "",
                    headers: {
                      "Content-Type": "application/json",
                      "x-api-key": "",
                    },
                  } satisfies ApiCommandOptions;
                }

                const includeOverrideHeader =
                  task.max_steps_per_run !== null &&
                  task.max_steps_per_run !== MAX_STEPS_DEFAULT;

                const headers: Record<string, string> = {
                  "Content-Type": "application/json",
                  "x-api-key": apiCredential ?? "<your-api-key>",
                };

                if (includeOverrideHeader) {
                  headers["x-max-steps-override"] = String(
                    task.max_steps_per_run,
                  );
                }

                return {
                  method: "POST",
                  url: `${runsApiBaseUrl}/run/tasks`,
                  body: buildTaskRunPayload(
                    createTaskRequestObject(task),
                    RunEngine.SkyvernV1,
                  ),
                  headers,
                } satisfies ApiCommandOptions;
              }}
              webhookDisabled={taskIsLoading || !taskHasTerminalState}
              onTestWebhook={() => setReplayOpen(true)}
            />
            <WebhookReplayDialog
              runId={task?.workflow_run_id ?? ""}
              disabled={taskIsLoading || !taskHasTerminalState}
              open={replayOpen}
              onOpenChange={setReplayOpen}
              hideTrigger
            />
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
                <Link to={`/tasks/create/retry/${task.task_id}`}>
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
                to={
                  env.useNewRunsUrl
                    ? `/runs/${workflowRun.workflow_run_id}`
                    : `/workflows/${workflow.workflow_permanent_id}/${workflowRun.workflow_run_id}/overview`
                }
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
          {webhookFailureReason}
        </>
      )}
      <SwitchBarNavigation
        options={[
          {
            label: "Actions",
            to: "actions",
          },
          {
            label: "Recording",
            to: "recording",
          },
          {
            label: "Parameters",
            to: "parameters",
          },
          {
            label: "Diagnostics",
            to: "diagnostics",
          },
        ]}
      />
      <Outlet />
    </div>
  );
}

export { TaskDetails };
