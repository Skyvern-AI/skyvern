import { getClient } from "@/api/AxiosClient";
import { StatusBadge } from "@/components/StatusBadge";
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
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/use-toast";
import { useApiCredential } from "@/hooks/useApiCredential";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { copyText } from "@/util/copyText";
import { apiBaseUrl } from "@/util/env";
import {
  CopyIcon,
  Pencil2Icon,
  PlayIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import fetchToCurl from "fetch-to-curl";
import { Link, NavLink, Outlet, useParams } from "react-router-dom";
import { statusIsFinalized, statusIsRunningOrQueued } from "../tasks/types";
import { useWorkflowQuery } from "./hooks/useWorkflowQuery";
import { useWorkflowRunQuery } from "./hooks/useWorkflowRunQuery";
import { cn } from "@/util/utils";

function WorkflowRun() {
  const { workflowRunId, workflowPermanentId } = useParams();
  const credentialGetter = useCredentialGetter();
  const apiCredential = useApiCredential();
  const queryClient = useQueryClient();

  const { data: workflow, isLoading: workflowIsLoading } = useWorkflowQuery({
    workflowPermanentId,
  });

  const { data: workflowRun, isLoading: workflowRunIsLoading } =
    useWorkflowRunQuery();

  const cancelWorkflowMutation = useMutation({
    mutationFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .post(`/workflows/runs/${workflowRunId}/cancel`)
        .then((response) => response.data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workflowRun", workflowRunId],
      });
      queryClient.invalidateQueries({
        queryKey: ["workflowRun", workflowPermanentId, workflowRunId],
      });
      toast({
        variant: "success",
        title: "Workflow Canceled",
        description: "The workflow has been successfully canceled.",
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

  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);

  const workflowRunIsFinalized = workflowRun && statusIsFinalized(workflowRun);

  const parameters = workflowRun?.parameters ?? {};

  const title = workflowIsLoading ? (
    <Skeleton className="h-9 w-48" />
  ) : (
    <h1 className="text-3xl">{workflow?.title}</h1>
  );

  const workflowFailureReason = workflowRun?.failure_reason ? (
    <div
      className="space-y-2 rounded-md border border-red-600 p-4"
      style={{
        backgroundColor: "rgba(220, 38, 38, 0.10)",
      }}
    >
      <div className="font-bold">Workflow Failure Reason</div>
      <div className="text-sm">{workflowRun.failure_reason}</div>
    </div>
  ) : null;

  return (
    <div className="space-y-8">
      <header className="flex justify-between">
        <div className="space-y-3">
          <div className="flex items-center gap-5">
            {title}
            {workflowRunIsLoading ? (
              <Skeleton className="h-8 w-28" />
            ) : workflowRun ? (
              <StatusBadge status={workflowRun?.status} />
            ) : null}
          </div>
          <h2 className="text-2xl text-slate-400">{workflowRunId}</h2>
        </div>

        <div className="flex gap-2">
          <Button
            variant="secondary"
            onClick={() => {
              if (!workflowRun) {
                return;
              }
              const curl = fetchToCurl({
                method: "POST",
                url: `${apiBaseUrl}/workflows/${workflowPermanentId}/run`,
                body: {
                  data: workflowRun?.parameters,
                  proxy_location: "RESIDENTIAL",
                },
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
          <Button asChild variant="secondary">
            <Link to={`/workflows/${workflowPermanentId}/edit`}>
              <Pencil2Icon className="mr-2 h-4 w-4" />
              Edit
            </Link>
          </Button>
          {workflowRunIsRunningOrQueued && (
            <Dialog>
              <DialogTrigger asChild>
                <Button variant="destructive">Cancel</Button>
              </DialogTrigger>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>Are you sure?</DialogTitle>
                  <DialogDescription>
                    Are you sure you want to cancel this workflow run?
                  </DialogDescription>
                </DialogHeader>
                <DialogFooter>
                  <DialogClose asChild>
                    <Button variant="secondary">Back</Button>
                  </DialogClose>
                  <Button
                    variant="destructive"
                    onClick={() => {
                      cancelWorkflowMutation.mutate();
                    }}
                    disabled={cancelWorkflowMutation.isPending}
                  >
                    {cancelWorkflowMutation.isPending && (
                      <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
                    )}
                    Cancel Workflow Run
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
          )}
          {workflowRunIsFinalized && (
            <Button asChild>
              <Link
                to={`/workflows/${workflowPermanentId}/run`}
                state={{
                  data: parameters,
                }}
              >
                <PlayIcon className="mr-2 h-4 w-4" />
                Rerun
              </Link>
            </Button>
          )}
        </div>
      </header>
      {workflowFailureReason}
      <div className="flex w-fit gap-2 rounded-sm border border-slate-700 p-2">
        <NavLink
          to="blocks"
          replace
          className={({ isActive }) => {
            return cn(
              "cursor-pointer rounded-sm px-3 py-2 hover:bg-slate-700",
              {
                "bg-slate-700": isActive,
              },
            );
          }}
        >
          Blocks
        </NavLink>
        <NavLink
          to="output"
          replace
          className={({ isActive }) => {
            return cn(
              "cursor-pointer rounded-sm px-3 py-2 hover:bg-slate-700",
              {
                "bg-slate-700": isActive,
              },
            );
          }}
        >
          Output
        </NavLink>
        <NavLink
          to="parameters"
          replace
          className={({ isActive }) => {
            return cn(
              "cursor-pointer rounded-sm px-3 py-2 hover:bg-slate-700",
              {
                "bg-slate-700": isActive,
              },
            );
          }}
        >
          Parameters
        </NavLink>
        <NavLink
          to="recording"
          replace
          className={({ isActive }) => {
            return cn(
              "cursor-pointer rounded-sm px-3 py-2 hover:bg-slate-700",
              {
                "bg-slate-700": isActive,
              },
            );
          }}
        >
          Recording
        </NavLink>
      </div>
      <Outlet />
    </div>
  );
}

export { WorkflowRun };
