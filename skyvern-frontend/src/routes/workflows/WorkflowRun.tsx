import { AxiosError } from "axios";
import { useEffect, useState } from "react";
import { getClient } from "@/api/AxiosClient";
import { ProxyLocation, Status } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import {
  SwitchBarNavigation,
  type SwitchBarNavigationOption,
} from "@/components/SwitchBarNavigation";
import { Button } from "@/components/ui/button";
import { Status404 } from "@/components/Status404";
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
import { runsApiBaseUrl } from "@/util/env";
import {
  CodeIcon,
  FileIcon,
  Pencil2Icon,
  PlayIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, Outlet, useSearchParams } from "react-router-dom";
import { statusIsCancellable, statusIsFinalized } from "../tasks/types";
import { useWorkflowRunWithWorkflowQuery } from "./hooks/useWorkflowRunWithWorkflowQuery";
import { WorkflowRunTimeline } from "./workflowRun/WorkflowRunTimeline";
import { useWorkflowRunTimelineQuery } from "./hooks/useWorkflowRunTimelineQuery";
import { findActiveItem } from "./workflowRun/workflowTimelineUtils";
import { Label } from "@/components/ui/label";
import { CodeEditor } from "./components/CodeEditor";
import { cn } from "@/util/utils";
import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { ApiWebhookActionsMenu } from "@/components/ApiWebhookActionsMenu";
import { WebhookReplayDialog } from "@/components/WebhookReplayDialog";
import { useFirstParam } from "@/hooks/useFirstParam";
import { type ApiCommandOptions } from "@/util/apiCommands";
import { useBlockScriptsQuery } from "@/routes/workflows/hooks/useBlockScriptsQuery";
import { constructCacheKeyValue } from "@/routes/workflows/editor/utils";
import { useCacheKeyValuesQuery } from "@/routes/workflows/hooks/useCacheKeyValuesQuery";
import { WorkflowRunStatusAlert } from "@/routes/workflows/workflowRun/WorkflowRunStatusAlert";

function WorkflowRun() {
  const [searchParams, setSearchParams] = useSearchParams();
  const embed = searchParams.get("embed");
  const isEmbedded = embed === "true";
  const active = searchParams.get("active");
  const workflowRunId = useFirstParam("workflowRunId", "runId");
  const credentialGetter = useCredentialGetter();
  const apiCredential = useApiCredential();
  const queryClient = useQueryClient();

  const {
    data: workflowRun,
    isLoading: workflowRunIsLoading,
    isFetched,
    error,
  } = useWorkflowRunWithWorkflowQuery();

  const status = (error as AxiosError | undefined)?.response?.status;
  const workflow = workflowRun?.workflow;
  const workflowPermanentId = workflow?.workflow_permanent_id;
  const cacheKey = workflow?.cache_key ?? "";
  const isFinalized = workflowRun ? statusIsFinalized(workflowRun) : null;

  const [hasPublishedCode, setHasPublishedCode] = useState(false);

  const [cacheKeyValue, setCacheKeyValue] = useState(
    cacheKey === ""
      ? ""
      : constructCacheKeyValue({ codeKey: cacheKey, workflow, workflowRun }),
  );

  const { data: cacheKeyValues } = useCacheKeyValuesQuery({
    cacheKey,
    debounceMs: 100,
    page: 1,
    workflowPermanentId,
  });

  useEffect(() => {
    setCacheKeyValue(
      constructCacheKeyValue({ codeKey: cacheKey, workflow, workflowRun }) ??
        cacheKeyValues?.values[0],
    );
  }, [cacheKey, cacheKeyValues, setCacheKeyValue, workflow, workflowRun]);

  const { data: blockScriptsPublished } = useBlockScriptsQuery({
    cacheKey,
    cacheKeyValue,
    workflowPermanentId,
    pollIntervalMs: !hasPublishedCode && !isFinalized ? 3000 : undefined,
    status: "published",
    workflowRunId: workflowRun?.workflow_run_id,
  });

  useEffect(() => {
    const keys = Object.keys(blockScriptsPublished ?? {});
    setHasPublishedCode(keys.length > 0);
  }, [blockScriptsPublished, setHasPublishedCode]);

  const { data: workflowRunTimeline } = useWorkflowRunTimelineQuery();
  const [replayOpen, setReplayOpen] = useState(false);

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

  const workflowRunIsCancellable =
    workflowRun && statusIsCancellable(workflowRun);

  const workflowRunIsFinalized = workflowRun && statusIsFinalized(workflowRun);
  const selection = findActiveItem(
    workflowRunTimeline ?? [],
    active,
    !!workflowRunIsFinalized,
  );
  const parameters = workflowRun?.parameters ?? {};
  const proxyLocation =
    workflowRun?.proxy_location ?? ProxyLocation.Residential;
  const maxScreenshotScrolls = workflowRun?.max_screenshot_scrolls ?? null;

  const title = workflowRunIsLoading ? (
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

  const failureTips: { match: (reason: string) => boolean; tip: string }[] = [
    {
      match: (reason) => reason.includes("Invalid master password"),
      tip: "Tip: If inputting the master password via Docker Compose or in any container environment, make sure to double any dollar signs and do not surround it with quotes.",
    },
    // Add more tips as needed
  ];

  const failureReason = workflowRun?.failure_reason;

  const matchedTips = failureReason
    ? failureTips
        .filter(({ match }) => match(failureReason))
        .map(({ tip }, index) => (
          <div key={index} className="text-sm italic text-red-700">
            {tip}
          </div>
        ))
    : null;

  const failureReasonTitle =
    workflowRun?.status === Status.Terminated
      ? "Termination Reason"
      : "Failure Reason";

  const workflowFailureReason = workflowRun?.failure_reason ? (
    <div
      className="space-y-2 rounded-md border border-red-600 p-4"
      style={{
        backgroundColor: "rgba(220, 38, 38, 0.10)",
      }}
    >
      <div className="font-bold">{failureReasonTitle}</div>
      <div className="text-sm">{workflowRun.failure_reason}</div>
      {matchedTips}
    </div>
  ) : null;

  function handleSetActiveItem(id: string) {
    searchParams.set("active", id);
    setSearchParams(searchParams, {
      replace: true,
    });
  }

  const isTaskv2Run = workflowRun && workflowRun.task_v2 !== null;

  const webhookFailureReasonData =
    workflowRun?.task_v2?.webhook_failure_reason ??
    workflowRun?.webhook_failure_reason;

  const webhookFailureReason = webhookFailureReasonData ? (
    <div className="space-y-4">
      <Label>Webhook Failure Reason</Label>
      <div className="rounded-md border border-yellow-600 p-4 text-sm">
        {webhookFailureReasonData}
      </div>
    </div>
  ) : null;

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
    (hasSomeExtractedInformation ||
      hasFileUrls ||
      hasTaskv2Output ||
      webhookFailureReasonData) &&
    workflowRun.status === Status.Completed;

  const isGeneratingCode = !isFinalized && !hasPublishedCode;

  const switchBarOptions: SwitchBarNavigationOption[] = [
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
    {
      label: "Code",
      to: "code",
      icon: !isGeneratingCode ? (
        <CodeIcon className="inline-block size-5" />
      ) : (
        <ReloadIcon className="inline-block size-5 animate-spin" />
      ),
    },
  ];

  if (status === 404) {
    return <Status404 />;
  }

  return (
    <div className="space-y-8">
      {!isEmbedded && (
        <header className="flex justify-between">
          <div className="space-y-3">
            <div className="mr-2 flex items-start gap-5">
              {title}
              {workflowRunIsLoading ? (
                <Skeleton className="h-8 w-28" />
              ) : workflowRun ? (
                <StatusBadge
                  className="mt-[0.27rem]"
                  status={workflowRun?.status}
                />
              ) : null}
            </div>
            <h2 className="text-2xl text-slate-400">{workflowRunId}</h2>
          </div>

          <div className="flex gap-2">
            <ApiWebhookActionsMenu
              getOptions={() => {
                // Build headers - x-max-steps-override is optional and can be added manually if needed
                const headers: Record<string, string> = {
                  "Content-Type": "application/json",
                  "x-api-key": apiCredential ?? "<your-api-key>",
                };

                const body: Record<string, unknown> = {
                  workflow_id: workflowPermanentId,
                  parameters: workflowRun?.parameters,
                  proxy_location: proxyLocation,
                };

                if (maxScreenshotScrolls !== null) {
                  body.max_screenshot_scrolls = maxScreenshotScrolls;
                }

                if (workflowRun?.webhook_callback_url) {
                  body.webhook_url = workflowRun.webhook_callback_url;
                }

                return {
                  method: "POST",
                  url: `${runsApiBaseUrl}/run/workflows`,
                  body,
                  headers,
                } satisfies ApiCommandOptions;
              }}
              webhookDisabled={workflowRunIsLoading || !workflowRunIsFinalized}
              onTestWebhook={() => setReplayOpen(true)}
            />
            <WebhookReplayDialog
              runId={workflowRunId ?? ""}
              disabled={workflowRunIsLoading || !workflowRunIsFinalized}
              open={replayOpen}
              onOpenChange={setReplayOpen}
              hideTrigger
            />
            <Button asChild variant="secondary">
              <Link to={`/workflows/${workflowPermanentId}/debug`}>
                <Pencil2Icon className="mr-2 h-4 w-4" />
                Edit
              </Link>
            </Button>
            {workflowRunIsCancellable && (
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
                    webhookCallbackUrl: workflowRun?.webhook_callback_url ?? "",
                    maxScreenshotScrolls,
                  }}
                >
                  <PlayIcon className="mr-2 h-4 w-4" />
                  Rerun
                </Link>
              </Button>
            )}
          </div>
        </header>
      )}
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
          {webhookFailureReason}
        </div>
      )}
      {workflowFailureReason}
      {!isEmbedded && (
        <div className="flex items-center justify-between">
          <SwitchBarNavigation options={switchBarOptions} />
          {workflowRun && (
            <WorkflowRunStatusAlert
              status={workflowRun.status}
              title={workflow?.title}
              visible={workflowRun && !isFinalized}
            />
          )}
        </div>
      )}
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
