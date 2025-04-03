import { getClient } from "@/api/AxiosClient";
import { ProxyLocation, Status } from "@/api/types";
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
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/use-toast";
import { useApiCredential } from "@/hooks/useApiCredential";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { copyText } from "@/util/copyText";
import { apiBaseUrl } from "@/util/env";
import {
  CopyIcon,
  FileIcon,
  Pencil2Icon,
  PlayIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import fetchToCurl from "fetch-to-curl";
import { Link, Outlet, useParams, useSearchParams } from "react-router-dom";
import { statusIsFinalized, statusIsRunningOrQueued } from "../tasks/types";
import { useWorkflowQuery } from "./hooks/useWorkflowQuery";
import { useWorkflowRunQuery } from "./hooks/useWorkflowRunQuery";
import { WorkflowRunTimeline } from "./workflowRun/WorkflowRunTimeline";
import { useWorkflowRunTimelineQuery } from "./hooks/useWorkflowRunTimelineQuery";
import { findActiveItem } from "./workflowRun/workflowTimelineUtils";
import { Label } from "@/components/ui/label";
import { CodeEditor } from "./components/CodeEditor";
import { cn } from "@/util/utils";
import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";

function WorkflowRun() {
  const [searchParams, setSearchParams] = useSearchParams();
  const active = searchParams.get("active");
  const { workflowRunId, workflowPermanentId } = useParams();
  const credentialGetter = useCredentialGetter();
  const apiCredential = useApiCredential();
  const queryClient = useQueryClient();

  const { data: workflow, isLoading: workflowIsLoading } = useWorkflowQuery({
    workflowPermanentId,
  });

  const {
    data: workflowRun,
    isLoading: workflowRunIsLoading,
    isFetched,
  } = useWorkflowRunQuery();

  const { data: workflowRunTimeline } = useWorkflowRunTimelineQuery();

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
  const selection = findActiveItem(
    workflowRunTimeline ?? [],
    active,
    !!workflowRunIsFinalized,
  );
  const parameters = workflowRun?.parameters ?? {};
  const proxyLocation =
    workflowRun?.proxy_location ?? ProxyLocation.Residential;

  const title = workflowIsLoading ? (
    <Skeleton className="h-9 w-48" />
  ) : (
    <h1 className="text-3xl">
      <Link
        className="hover:underline hover:underline-offset-2"
        to={`/workflows/${workflowPermanentId}/runs`}
      >
        {workflow?.title}
      </Link>
    </h1>
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

  function handleSetActiveItem(id: string) {
    searchParams.set("active", id);
    setSearchParams(searchParams, {
      replace: true,
    });
  }

  const isTaskv2Run = workflowRun && workflowRun.task_v2 !== null;

  const outputs = workflowRun?.outputs;
  const extractedInformation =
    typeof outputs === "object" &&
    outputs !== null &&
    "extracted_information" in outputs
      ? (outputs.extracted_information as Record<string, unknown>)
      : null;

  const hasSomeExtractedInformation = extractedInformation
    ? Object.values(extractedInformation).some((value) => value !== null)
    : false;

  const hasTaskv2Output = Boolean(isTaskv2Run && workflowRun.task_v2?.output);

  const hasFileUrls =
    isFetched &&
    workflowRun &&
    workflowRun.downloaded_file_urls &&
    workflowRun.downloaded_file_urls.length > 0;
  const fileUrls = hasFileUrls
    ? (workflowRun.downloaded_file_urls as string[])
    : [];

  const showBoth =
    (hasSomeExtractedInformation || hasTaskv2Output) && hasFileUrls;

  const showOutputSection =
    workflowRunIsFinalized &&
    (hasSomeExtractedInformation || hasFileUrls || hasTaskv2Output) &&
    workflowRun.status === Status.Completed;

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
          {workflowRunIsFinalized && !isTaskv2Run && (
            <Button asChild>
              <Link
                to={`/workflows/${workflowPermanentId}/run`}
                state={{
                  data: parameters,
                  proxyLocation,
                }}
              >
                <PlayIcon className="mr-2 h-4 w-4" />
                Rerun
              </Link>
            </Button>
          )}
        </div>
      </header>
      {showOutputSection && (
        <div
          className={cn("grid gap-4 rounded-lg bg-slate-elevation1 p-4", {
            "grid-cols-2": showBoth,
          })}
        >
          {(hasSomeExtractedInformation || hasTaskv2Output) && (
            <div className="space-y-4">
              <Label>
                {hasTaskv2Output ? "Output" : "Extracted Information"}
              </Label>
              <CodeEditor
                language="json"
                value={
                  hasTaskv2Output
                    ? JSON.stringify(workflowRun.task_v2?.output, null, 2)
                    : JSON.stringify(extractedInformation, null, 2)
                }
                readOnly
                maxHeight="250px"
              />
            </div>
          )}
          {hasFileUrls && (
            <div className="space-y-4">
              <Label>Downloaded Files</Label>
              <ScrollArea>
                <ScrollAreaViewport className="max-h-[250px] space-y-2">
                  {fileUrls.length > 0 ? (
                    fileUrls.map((url, index) => {
                      return (
                        <div key={url} title={url} className="flex gap-2">
                          <FileIcon className="size-6" />
                          <a
                            href={url}
                            className="underline underline-offset-4"
                          >
                            <span>{`File ${index + 1}`}</span>
                          </a>
                        </div>
                      );
                    })
                  ) : (
                    <div className="text-sm">No files downloaded</div>
                  )}
                </ScrollAreaViewport>
              </ScrollArea>
            </div>
          )}
        </div>
      )}
      {workflowFailureReason}
      <SwitchBarNavigation
        options={[
          {
            label: "Overview",
            to: "overview",
          },
          {
            label: "Output",
            to: "output",
          },
          {
            label: "Parameters",
            to: "parameters",
          },
          {
            label: "Recording",
            to: "recording",
          },
        ]}
      />
      <div className="flex h-[42rem] gap-6">
        <div className="w-2/3">
          <Outlet />
        </div>
        <div className="w-1/3">
          <WorkflowRunTimeline
            activeItem={selection}
            onActionItemSelected={(item) => {
              handleSetActiveItem(item.action.action_id);
            }}
            onBlockItemSelected={(item) => {
              handleSetActiveItem(item.workflow_run_block_id);
            }}
            onLiveStreamSelected={() => {
              handleSetActiveItem("stream");
            }}
            onObserverThoughtCardSelected={(item) => {
              handleSetActiveItem(item.thought_id);
            }}
          />
        </div>
      </div>
    </div>
  );
}

export { WorkflowRun };
