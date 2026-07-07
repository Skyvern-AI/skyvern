import { AxiosError } from "axios";
import { useCallback, useEffect, useRef, useState } from "react";
import { getClient } from "@/api/AxiosClient";
import { ProxyLocation, Status } from "@/api/types";
import { FailureCategoryBadge } from "@/components/FailureCategoryBadge";
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
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import {
  CodeIcon,
  FileIcon,
  Pencil2Icon,
  PlayIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Link,
  Navigate,
  Outlet,
  useNavigate,
  useSearchParams,
} from "react-router-dom";
import {
  statusIsAFailureType,
  statusIsCancellable,
  statusIsFinalized,
} from "../tasks/types";
import { useWorkflowRunWithWorkflowQuery } from "./hooks/useWorkflowRunWithWorkflowQuery";
import { useRefreshOnboardingOnRunCompletion } from "./hooks/useRefreshOnboardingOnRunCompletion";
import { WorkflowRunBlockDetail } from "./workflowRun/WorkflowRunBlockDetail";
import { WorkflowRunTimeline } from "./workflowRun/WorkflowRunTimeline";
import { useWorkflowRunTimelineQuery } from "./hooks/useWorkflowRunTimelineQuery";
import {
  findActiveItem,
  parseActiveIterationParam,
} from "./workflowRun/workflowTimelineUtils";
import { pickDownloadedFileFilename } from "./workflowRun/blockDownloadedFiles";
import { isBlockItem } from "./types/workflowRunTypes";
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
import { WorkflowRunVerificationCodeForm } from "@/routes/workflows/workflowRun/WorkflowRunVerificationCodeForm";
import { ScriptUpdateCard } from "@/routes/workflows/workflowRun/ScriptUpdateCard";
import { useFallbackEpisodesQuery } from "@/routes/workflows/hooks/useFallbackEpisodesQuery";
import { useRunsQuery } from "@/hooks/useRunsQuery";
import { useOnboardingStateOptional } from "@/store/onboarding/useOnboardingState";
import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { workflowEditorPath } from "@/routes/workflows/studioNavigation";
import { FirstRunRecoveryGuidance } from "@/components/onboarding/FirstRunRecoveryGuidance";
import { useFeatureFlagVariantKey } from "posthog-js/react";
import { EXPERIMENT } from "@/util/onboarding/experimentConfig";
import { isFirstFailedRunRecoveryEligible } from "@/util/onboarding/rolloutGating";

function WorkflowRunRightColumn({
  activeItem,
  activeIteration,
  timeline,
  timelineReady,
  onSetActiveItem,
  onSetActiveIteration,
}: {
  activeItem: ReturnType<typeof findActiveItem>;
  activeIteration: number | null;
  timeline: NonNullable<ReturnType<typeof useWorkflowRunTimelineQuery>["data"]>;
  timelineReady: boolean;
  onSetActiveItem: (id: string) => void;
  onSetActiveIteration: (loopBlockId: string, iterationIndex: number) => void;
}) {
  return (
    <div className="grid min-h-0 w-[clamp(28rem,34vw,36rem)] shrink-0 grid-rows-[minmax(0,1fr)_minmax(0,1fr)] gap-3">
      <div className="min-h-0 w-full overflow-hidden">
        <WorkflowRunTimeline
          activeItem={activeItem}
          activeIteration={activeIteration}
          onActionItemSelected={(item) => {
            onSetActiveItem(item.action.action_id);
          }}
          onBlockItemSelected={(item) => {
            onSetActiveItem(item.workflow_run_block_id);
          }}
          onThoughtItemSelected={(item) => {
            onSetActiveItem(item.thought_id);
          }}
          onLiveStreamSelected={() => {
            onSetActiveItem("stream");
          }}
          onIterationSelected={(loopBlock, iterationIndex) => {
            onSetActiveIteration(
              loopBlock.workflow_run_block_id,
              iterationIndex,
            );
          }}
        />
      </div>
      <div className="flex min-h-0 w-full flex-col overflow-hidden rounded-md border border-slate-700 bg-slate-elevation1">
        <WorkflowRunBlockDetail
          activeItem={activeItem}
          activeIteration={activeIteration}
          timeline={timeline}
          timelineReady={timelineReady}
          onThoughtSelect={(thought) => {
            onSetActiveItem(thought.thought_id);
          }}
        />
      </div>
    </div>
  );
}

function WorkflowRun() {
  const [searchParams, setSearchParams] = useSearchParams();
  const searchParamsRef = useRef(searchParams);
  searchParamsRef.current = searchParams;
  const embed = searchParams.get("embed");
  const isEmbedded = embed === "true";
  const active = searchParams.get("active");
  const iterationParam = searchParams.get("iteration");
  const activeIteration = parseActiveIterationParam(iterationParam);
  const workflowRunId = useFirstParam("workflowRunId", "runId");
  const workflowPermanentIdParam = useFirstParam("workflowPermanentId");
  const credentialGetter = useCredentialGetter();
  const apiCredential = useApiCredential();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const studioEnabled = useWorkflowStudioEnabled();
  const onboarding = useOnboardingStateOptional();

  const {
    data: workflowRun,
    isLoading: workflowRunIsLoading,
    isFetched,
    error,
  } = useWorkflowRunWithWorkflowQuery();

  useRefreshOnboardingOnRunCompletion(workflowRun);

  const status = (error as AxiosError | undefined)?.response?.status;
  const workflow = workflowRun?.workflow;
  const workflowPermanentId =
    workflowPermanentIdParam ?? workflow?.workflow_permanent_id;
  const cacheKey = workflow?.cache_key ?? "";
  const isFinalized = workflowRun ? statusIsFinalized(workflowRun) : null;
  const isWorkflowDeleted = Boolean(workflow?.deleted_at);

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
    workflowPermanentId: isWorkflowDeleted ? undefined : workflowPermanentId,
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
    workflowPermanentId: isWorkflowDeleted ? undefined : workflowPermanentId,
    pollIntervalMs: !hasPublishedCode && !isFinalized ? 3000 : undefined,
    status: "published",
    workflowRunId: workflowRun?.workflow_run_id,
  });

  useEffect(() => {
    const keys = Object.keys(blockScriptsPublished?.blocks ?? {});
    setHasPublishedCode(
      keys.length > 0 || Boolean(blockScriptsPublished?.main_script),
    );
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
        title: "Agent Canceled",
        description: "The agent has been successfully canceled.",
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

  const { data: fallbackEpisodes } = useFallbackEpisodesQuery({
    workflowPermanentId,
    workflowRunId: workflowRun?.workflow_run_id,
    enabled: workflowRunIsFinalized === true && !isWorkflowDeleted,
  });
  const finallyBlockLabel =
    workflow?.workflow_definition?.finally_block_label ?? null;
  const selection = findActiveItem(
    workflowRunTimeline ?? [],
    active,
    !!workflowRunIsFinalized,
    finallyBlockLabel,
  );
  const parameters = workflowRun?.parameters ?? {};
  const proxyLocation =
    workflowRun?.proxy_location ?? ProxyLocation.Residential;
  const maxScreenshotScrolls = workflowRun?.max_screenshot_scrolls ?? null;

  const title = workflowRunIsLoading ? (
    <Skeleton className="h-9 w-48" />
  ) : isWorkflowDeleted ? (
    <h1 className="text-3xl">{workflow!.title}</h1>
  ) : (
    <h1 className="text-3xl">
      <Link
        className="hover:underline hover:underline-offset-2"
        to={`/agents/${workflowPermanentId}/runs`}
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

  const finallyBlockInTimeline = finallyBlockLabel
    ? workflowRunTimeline?.find(
        (item) => isBlockItem(item) && item.block.label === finallyBlockLabel,
      )
    : null;

  const finallyBlockStatus =
    finallyBlockInTimeline && isBlockItem(finallyBlockInTimeline)
      ? finallyBlockInTimeline.block.status
      : null;

  const shouldShowFinallyNote =
    (workflowRun?.status === Status.Terminated ||
      workflowRun?.status === Status.Failed) &&
    finallyBlockLabel &&
    finallyBlockInTimeline;

  // Gate the first-failed-run guidance. first_run_at is stamped on any first
  // terminal run, so derive "first run" from the runs list and assert this run
  // is that single run.
  const isFailureRun = workflowRun
    ? statusIsAFailureType({ status: workflowRun.status })
    : false;
  const onboardingFlagVariant = useFeatureFlagVariantKey(EXPERIMENT.flagKey);
  // Gate on the rollout arm so a 0% rollout / rollback hides the recovery surface.
  const firstFailedRunGateEnabled = isFirstFailedRunRecoveryEligible({
    flagVariant: onboardingFlagVariant,
    isNewUser: onboarding?.isNewUser === true,
    isFailureRun,
    hasFailureReason: Boolean(workflowRun?.failure_reason),
  });
  const { data: recentRuns } = useRunsQuery({
    page: 1,
    pageSize: 2,
    enabled: firstFailedRunGateEnabled,
  });
  const showFirstFailedRunRecovery =
    firstFailedRunGateEnabled &&
    recentRuns?.length === 1 &&
    recentRuns[0]?.run_id === workflowRun?.workflow_run_id;

  const handleFirstFailedRunRetry = useCallback(() => {
    navigate(`/agents/${workflowPermanentId}/run`, {
      state: {
        data: workflowRun?.parameters ?? {},
        proxyLocation,
        webhookCallbackUrl: workflowRun?.webhook_callback_url ?? "",
        maxScreenshotScrolls,
        runWith: workflowRun?.run_with ?? "agent",
        browserProfileId: workflowRun?.browser_profile_id ?? null,
      },
    });
  }, [
    navigate,
    workflowPermanentId,
    proxyLocation,
    maxScreenshotScrolls,
    workflowRun?.parameters,
    workflowRun?.webhook_callback_url,
    workflowRun?.run_with,
    workflowRun?.browser_profile_id,
  ]);

  const workflowFailureReason = workflowRun?.failure_reason ? (
    <div className="space-y-2 rounded-md border border-red-600 bg-error-light p-4">
      <div className="flex items-center gap-2">
        <div className="font-bold">{failureReasonTitle}</div>
        <FailureCategoryBadge failureCategory={workflowRun.failure_category} />
      </div>
      <div className="text-sm">{workflowRun.failure_reason}</div>
      {matchedTips}
      {showFirstFailedRunRecovery && (
        <FirstRunRecoveryGuidance
          surface="runs"
          failureCategory={workflowRun.failure_category?.[0]?.category ?? null}
          workflowPermanentId={workflowPermanentId}
          onRetry={handleFirstFailedRunRetry}
        />
      )}
      {shouldShowFinallyNote && (
        <div className="mt-2 flex items-center gap-2 rounded bg-amber-500/20 px-3 py-2 text-sm text-amber-200">
          <span className="font-medium">Note:</span>
          <span>
            "Execute on any outcome" block ({finallyBlockLabel}){" "}
            {finallyBlockStatus === Status.Completed
              ? "completed successfully"
              : finallyBlockStatus === Status.Failed
                ? "failed"
                : "ran"}
            .
          </span>
        </div>
      )}
    </div>
  ) : null;

  const updateSearchParams = useCallback(
    (mutate: (params: URLSearchParams) => void) => {
      setSearchParams(
        () => {
          const next = new URLSearchParams(searchParamsRef.current);
          mutate(next);
          searchParamsRef.current = next;
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  function handleSetActiveItem(id: string) {
    updateSearchParams((next) => {
      next.set("active", id);
      next.delete("iteration");
    });
  }

  function handleSetActiveIteration(
    loopBlockId: string,
    iterationIndex: number,
  ) {
    updateSearchParams((next) => {
      next.set("active", loopBlockId);
      next.set("iteration", String(iterationIndex));
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
  // Prefer the rich downloaded_files array (carries filename, checksum, size)
  // when the backend sends it; falls back to URL parsing otherwise.
  const filenameByUrl = new Map<string, string>();
  if (workflowRun?.downloaded_files) {
    for (const file of workflowRun.downloaded_files) {
      if (file.filename) {
        filenameByUrl.set(file.url, file.filename);
      }
    }
  }

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
      label: "Inputs",
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

  // With the preview on, route legacy run links into the studio Run tab
  // (preserving the selected item); flag-off keeps this legacy run view.
  if (studioEnabled && !isEmbedded && workflowRunId && workflowPermanentId) {
    const studioParams = new URLSearchParams();
    studioParams.set("wr", workflowRunId);
    if (active) {
      studioParams.set("active", active);
    }
    if (iterationParam) {
      studioParams.set("iteration", iterationParam);
    }
    return (
      <Navigate
        to={`/agents/${workflowPermanentId}/studio?${studioParams.toString()}`}
        replace
      />
    );
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
            <h2 className="text-2xl text-neutral-600 dark:text-slate-400">
              {workflowRunId}
            </h2>
            {workflowRun &&
              (workflowRun.started_at ||
                workflowRun.finished_at ||
                isWorkflowDeleted) && (
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-neutral-600 dark:text-slate-400">
                  {workflowRun.started_at && (
                    <span title={basicTimeFormat(workflowRun.started_at)}>
                      Started: {basicLocalTimeFormat(workflowRun.started_at)}
                    </span>
                  )}
                  {workflowRun.finished_at && (
                    <span title={basicTimeFormat(workflowRun.finished_at)}>
                      Finished: {basicLocalTimeFormat(workflowRun.finished_at)}
                    </span>
                  )}
                  {isWorkflowDeleted && (
                    <span title={basicTimeFormat(workflow!.deleted_at!)}>
                      Agent deleted on{" "}
                      {basicLocalTimeFormat(workflow!.deleted_at!)}
                    </span>
                  )}
                </div>
              )}
            {workflowRun?.browser_session_id && (
              <Link
                className="font-mono text-sm text-neutral-600 hover:text-neutral-950 hover:underline hover:underline-offset-2 dark:text-slate-400 dark:hover:text-slate-200"
                to={`/browser-session/${workflowRun.browser_session_id}/stream`}
              >
                Browser Session: {workflowRun.browser_session_id}
              </Link>
            )}
            {workflowRun?.browser_profile_id && (
              <Link
                className="font-mono text-sm text-neutral-600 hover:text-neutral-950 hover:underline hover:underline-offset-2 dark:text-slate-400 dark:hover:text-slate-200"
                to={`/browser-profiles/${workflowRun.browser_profile_id}`}
              >
                Browser Profile: {workflowRun.browser_profile_id}
              </Link>
            )}
          </div>

          <div className="flex gap-2">
            {!isWorkflowDeleted && (
              <>
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
                  webhookDisabled={
                    workflowRunIsLoading || !workflowRunIsFinalized
                  }
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
                  <Link
                    to={workflowEditorPath(
                      workflowPermanentId ?? "",
                      studioEnabled,
                    )}
                    data-testid="workflow-open-editor-link"
                  >
                    <Pencil2Icon className="mr-2 h-4 w-4" />
                    Edit
                  </Link>
                </Button>
              </>
            )}
            {workflowRunIsCancellable && (
              <Dialog>
                <DialogTrigger asChild>
                  <Button variant="destructive">Cancel</Button>
                </DialogTrigger>
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>Are you sure?</DialogTitle>
                    <DialogDescription>
                      Are you sure you want to cancel this agent run?
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
                      Cancel Agent Run
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            )}
            {workflowRunIsFinalized && !isTaskv2Run && !isWorkflowDeleted && (
              <Button asChild>
                <Link
                  to={`/agents/${workflowPermanentId}/run`}
                  state={{
                    data: parameters,
                    proxyLocation,
                    webhookCallbackUrl: workflowRun?.webhook_callback_url ?? "",
                    maxScreenshotScrolls,
                    runWith: workflowRun?.run_with ?? "agent",
                    browserProfileId: workflowRun?.browser_profile_id ?? null,
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
      {/* 2FA Verification Code Form - shown when workflow is waiting for a code */}
      <WorkflowRunVerificationCodeForm />
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
                    fileUrls.map((url) => {
                      const filename = pickDownloadedFileFilename(
                        url,
                        filenameByUrl,
                      );
                      return (
                        <div key={url} title={url} className="flex gap-2">
                          <FileIcon className="size-6" />
                          <a
                            href={url}
                            className="underline underline-offset-4"
                          >
                            <span>{filename}</span>
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
      {fallbackEpisodes && fallbackEpisodes.episodes.length > 0 && (
        <ScriptUpdateCard
          episodes={fallbackEpisodes.episodes}
          scriptId={blockScriptsPublished?.script_id}
        />
      )}
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
      {/* 18rem accounts for nav, run metadata, tabs, and page gutters above this work area. */}
      <div className="flex h-[calc(100vh-18rem)] max-h-[52rem] min-h-[34rem] gap-6">
        <div className="min-w-0 flex-[2]">
          <Outlet />
        </div>
        <WorkflowRunRightColumn
          activeItem={selection}
          activeIteration={activeIteration}
          timeline={workflowRunTimeline ?? []}
          timelineReady={workflowRunTimeline !== undefined}
          onSetActiveItem={handleSetActiveItem}
          onSetActiveIteration={handleSetActiveIteration}
        />
      </div>
    </div>
  );
}

export { WorkflowRun };
