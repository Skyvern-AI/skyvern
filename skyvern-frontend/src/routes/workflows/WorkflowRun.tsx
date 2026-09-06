import { AxiosError } from "axios";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getClient } from "@/api/AxiosClient";
import { ProxyLocation, Status } from "@/api/types";
import { FailureCategoryBadge } from "@/components/FailureCategoryBadge";
import { StatusBadge } from "@/components/StatusBadge";
import { CredentialFallbackRetryBadge } from "@/components/CredentialFallbackRetryBadge";
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
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  runOverviewScreenshotLocation,
  runViewTabBasePath,
} from "@/routes/runs/runViewTabBasePath";
import { statusIsCancellable, statusIsFinalized } from "../tasks/types";
import { useWorkflowRunWithWorkflowQuery } from "./hooks/useWorkflowRunWithWorkflowQuery";
import { useRefreshOnboardingOnRunCompletion } from "./hooks/useRefreshOnboardingOnRunCompletion";
import { ResizableTimelineSplit } from "./workflowRun/ResizableTimelineSplit";
import { WorkflowRunBlockDetail } from "./workflowRun/WorkflowRunBlockDetail";
import { WorkflowRunTimeline } from "./workflowRun/WorkflowRunTimeline";
import { useWorkflowRunTimelineQuery } from "./hooks/useWorkflowRunTimelineQuery";
import {
  findActiveItem,
  parseActiveIterationParam,
} from "./workflowRun/workflowTimelineUtils";
import { ArtifactDownloadLink } from "@/components/ArtifactDownloadLink";
import { pickDownloadedFileFilename } from "./workflowRun/blockDownloadedFiles";
import { findRunCodeBlockFailure } from "./workflowRun/codeBlockFailure";
import { matchFailureTips } from "./studio/runview/failureTips";
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
import { usePageSlots } from "@/store/PageSlots";
import { useOnboardingStateOptional } from "@/store/onboarding/useOnboardingState";
import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { workflowEditorPath } from "@/routes/workflows/studioNavigation";
import {
  FirstRunRecoveryGuidance,
  shouldShowRecoveryGuidance,
} from "@/components/onboarding/FirstRunRecoveryGuidance";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import {
  getRecoveryGuidanceRetryNavigation,
  RecoveryGuidanceTelemetry,
  retryRunHasStarted,
  type RecoveryGuidanceTelemetryContext,
} from "@/util/onboarding/recoveryGuidanceTelemetry";
import { RunTagsEditor } from "@/routes/tasks/components/tagging/RunTagsEditor";
import { getRerunNavigationState, shouldPollForGeneratedCode } from "./utils";

const RECOVERY_GUIDANCE_TREATMENT_SURFACE_FLAG =
  "RECOVERY_GUIDANCE_TREATMENT_SURFACE";

function WorkflowRunRightColumn({
  workflowRunId,
  activeItem,
  activeIteration,
  timeline,
  timelineReady,
  onSetActiveItem,
  onSetActiveIteration,
  onViewScreenshot,
}: {
  workflowRunId: string | undefined;
  activeItem: ReturnType<typeof findActiveItem>;
  activeIteration: number | null;
  timeline: NonNullable<ReturnType<typeof useWorkflowRunTimelineQuery>["data"]>;
  timelineReady: boolean;
  onSetActiveItem: (id: string) => void;
  onSetActiveIteration: (loopBlockId: string, iterationIndex: number) => void;
  onViewScreenshot: (workflowRunBlockId: string) => void;
}) {
  return (
    <ResizableTimelineSplit
      className="w-[clamp(28rem,34vw,36rem)] shrink-0"
      top={
        <div className="min-h-0 w-full overflow-hidden">
          <WorkflowRunTimeline
            workflowRunId={workflowRunId}
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
      }
      bottom={
        <div className="flex min-h-0 w-full flex-col overflow-hidden rounded-md border border-border bg-slate-elevation1">
          <WorkflowRunBlockDetail
            workflowRunId={workflowRunId}
            activeItem={activeItem}
            activeIteration={activeIteration}
            timeline={timeline}
            timelineReady={timelineReady}
            onViewScreenshot={onViewScreenshot}
            onThoughtSelect={(thought) => {
              onSetActiveItem(thought.thought_id);
            }}
          />
        </div>
      }
    />
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
  const runBasePath =
    runViewTabBasePath(useParams()) ?? `/runs/${workflowRunId}`;
  const workflowPermanentIdParam = useFirstParam("workflowPermanentId");
  const credentialGetter = useCredentialGetter();
  const apiCredential = useApiCredential();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const studioEnabled = useWorkflowStudioEnabled();
  const onboarding = useOnboardingStateOptional();
  const { workflowRunMilestoneCard: WorkflowRunMilestoneCard } = usePageSlots();

  const {
    data: workflowRun,
    isLoading: workflowRunIsLoading,
    isPlaceholderData: workflowRunIsPlaceholder,
    isFetched,
    error,
  } = useWorkflowRunWithWorkflowQuery();

  useRefreshOnboardingOnRunCompletion(workflowRun);
  const recoveryGuidanceRetry = getRecoveryGuidanceRetryNavigation(
    location.state,
  );
  const reportedRecoveryRetryStartRef = useRef<string | null>(null);

  useEffect(() => {
    if (
      !recoveryGuidanceRetry ||
      !retryRunHasStarted({
        retryRunId: recoveryGuidanceRetry.retryRunId,
        observedRunId: workflowRun?.workflow_run_id,
        status: workflowRun?.status,
        startedAt: workflowRun?.started_at,
      }) ||
      reportedRecoveryRetryStartRef.current === recoveryGuidanceRetry.retryRunId
    ) {
      return;
    }
    reportedRecoveryRetryStartRef.current = recoveryGuidanceRetry.retryRunId;
    RecoveryGuidanceTelemetry.retryStarted(
      recoveryGuidanceRetry,
      recoveryGuidanceRetry.retryRunId,
    );
  }, [
    recoveryGuidanceRetry,
    workflowRun?.started_at,
    workflowRun?.status,
    workflowRun?.workflow_run_id,
  ]);

  const status = (error as AxiosError | undefined)?.response?.status;
  const workflow = workflowRun?.workflow;
  const workflowPermanentId =
    workflowPermanentIdParam ?? workflow?.workflow_permanent_id;
  const cacheKey = workflow?.cache_key ?? "";
  const isFinalized = workflowRun ? statusIsFinalized(workflowRun) : null;
  const isWorkflowDeleted = Boolean(workflow?.deleted_at);

  const [hasPublishedCode, setHasPublishedCode] = useState(false);
  const isGeneratingCode = shouldPollForGeneratedCode(
    workflow,
    isFinalized,
    hasPublishedCode,
  );

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
    pollIntervalMs: isGeneratingCode ? 3000 : undefined,
    status: "published",
    workflowRunId: workflowRun?.workflow_run_id,
  });

  useEffect(() => {
    const keys = Object.keys(blockScriptsPublished?.blocks ?? {});
    setHasPublishedCode(
      keys.length > 0 || Boolean(blockScriptsPublished?.main_script),
    );
  }, [blockScriptsPublished, setHasPublishedCode]);

  const { data: retainedTimeline, isPlaceholderData: timelineIsPlaceholder } =
    useWorkflowRunTimelineQuery();
  const workflowRunTimeline = timelineIsPlaceholder
    ? undefined
    : retainedTimeline;
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

  const statusUnavailable = Boolean(error);
  const workflowRunIsCancellable =
    !statusUnavailable && workflowRun && statusIsCancellable(workflowRun);

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

  const failureReason = workflowRun?.failure_reason;

  const matchedTips = matchFailureTips(failureReason ?? null).map((tip) => (
    <div key={tip} className="text-sm italic text-red-700">
      {tip}
    </div>
  ));

  const codeFailure = findRunCodeBlockFailure(
    failureReason,
    workflowRunTimeline,
    finallyBlockLabel,
  );

  const failureReasonTitle =
    workflowRun?.status === Status.Terminated
      ? "Termination Reason"
      : "Failure Reason";
  const siblingRunLinkClassName =
    "font-mono text-sm text-neutral-600 hover:text-neutral-950 hover:underline hover:underline-offset-2 dark:text-slate-400 dark:hover:text-slate-200";

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

  const recoveryGuidanceAssignment =
    onboarding?.recoveryGuidanceAssignment ?? null;
  const recoveryGuidanceTreatmentSurfaceEnabled =
    useFeatureFlag(RECOVERY_GUIDANCE_TREATMENT_SURFACE_FLAG) === true;
  const recoveryGuidanceTelemetryContext =
    useMemo<RecoveryGuidanceTelemetryContext | null>(() => {
      if (!recoveryGuidanceAssignment || !workflowRun) {
        return null;
      }
      return {
        organizationId: recoveryGuidanceAssignment.organization_id,
        experimentVersion: recoveryGuidanceAssignment.experiment_version,
        arm: recoveryGuidanceAssignment.arm,
        eligibleRunId: recoveryGuidanceAssignment.eligible_run_id,
        failureCategory: workflowRun.failure_category?.[0]?.category ?? null,
      };
    }, [recoveryGuidanceAssignment, workflowRun]);
  // This is a new, intentionally unconfigured flag. Its unknown/default value
  // is false, so neither arm gains a visible surface during instrumentation.
  const showFirstFailedRunRecovery = shouldShowRecoveryGuidance({
    assignment: recoveryGuidanceAssignment,
    workflowRunId: workflowRun?.workflow_run_id,
    treatmentSurfaceEnabled: recoveryGuidanceTreatmentSurfaceEnabled,
  });

  const handleFirstFailedRunRetry = useCallback(() => {
    navigate(`/agents/${workflowPermanentId}/run`, {
      state: {
        data: workflowRun?.parameters ?? {},
        proxyLocation,
        webhookCallbackUrl: workflowRun?.webhook_callback_url ?? "",
        maxScreenshotScrolls,
        runWith: workflowRun?.run_with ?? "agent",
        browserProfileId: workflowRun?.browser_profile_id ?? null,
        ...(recoveryGuidanceTelemetryContext
          ? { recoveryGuidanceRetry: recoveryGuidanceTelemetryContext }
          : {}),
      },
    });
  }, [
    navigate,
    workflowPermanentId,
    proxyLocation,
    maxScreenshotScrolls,
    recoveryGuidanceTelemetryContext,
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
      {codeFailure ? (
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-1.5 text-sm font-medium">
            <span>{codeFailure.title}</span>
            {codeFailure.line !== null ? (
              <span className="shrink-0 rounded border border-red-600/40 px-1.5 py-0.5 text-[10px] tabular-nums">
                line {codeFailure.line}
              </span>
            ) : null}
            {codeFailure.code ? (
              <span className="shrink-0 rounded border border-red-600/40 px-1.5 py-0.5 font-mono text-[10px]">
                {codeFailure.code}
              </span>
            ) : null}
          </div>
          <div className="text-sm">{codeFailure.guidance}</div>
        </div>
      ) : null}
      <div className="text-sm">{workflowRun.failure_reason}</div>
      {matchedTips}
      {showFirstFailedRunRecovery && recoveryGuidanceTelemetryContext && (
        <FirstRunRecoveryGuidance
          telemetryContext={recoveryGuidanceTelemetryContext}
          workflowPermanentId={workflowPermanentId}
          onRetry={handleFirstFailedRunRetry}
        />
      )}
      {shouldShowFinallyNote && (
        <div className="mt-2 flex items-center gap-2 rounded bg-amber-500/20 px-3 py-2 text-sm text-amber-700 dark:text-amber-200">
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

  const handleViewScreenshot = useCallback(
    (workflowRunBlockId: string) => {
      const destination = runOverviewScreenshotLocation(
        runBasePath,
        searchParamsRef.current.toString(),
        workflowRunBlockId,
      );
      searchParamsRef.current = new URLSearchParams(destination.search);
      navigate(destination, { replace: true });
    },
    [navigate, runBasePath],
  );

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

  const switchBarOptions: SwitchBarNavigationOption[] = [
    {
      label: "Overview",
      to: `${runBasePath}/overview`,
    },
    {
      label: "Output",
      to: `${runBasePath}/output`,
    },
    {
      label: "Inputs",
      to: `${runBasePath}/parameters`,
    },
    {
      label: "Recording",
      to: `${runBasePath}/recording`,
    },
    {
      label: "Code",
      to: `${runBasePath}/code`,
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

  // With the preview on, route legacy run links into the studio run view under
  // the short /runs/{wr} URL (preserving the selected item); flag-off keeps this
  // legacy run view. The run id lives in the path, so it stays out of the query.
  if (studioEnabled && !isEmbedded && workflowRunId && workflowPermanentId) {
    const studioParams = new URLSearchParams();
    if (active) {
      studioParams.set("active", active);
    }
    if (iterationParam) {
      studioParams.set("iteration", iterationParam);
    }
    const search = studioParams.toString();
    return (
      <Navigate
        to={`/runs/${workflowRunId}${search ? `?${search}` : ""}`}
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
              ) : workflowRun && !statusUnavailable ? (
                <div className="mt-[0.27rem] flex items-center gap-2">
                  <StatusBadge status={workflowRun?.status} />
                  <CredentialFallbackRetryBadge
                    retriedFromWorkflowRunId={
                      workflowRun.retried_from_workflow_run_id
                    }
                  />
                </div>
              ) : null}
            </div>
            <h2 className="text-2xl text-neutral-600 dark:text-slate-400">
              {workflowRunId}
            </h2>
            {workflowRunId ? (
              <RunTagsEditor workflowRunId={workflowRunId} />
            ) : null}
            {workflowRun && workflowPermanentId && (
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                {workflowRun.retried_from_workflow_run_id && (
                  <Link
                    className={siblingRunLinkClassName}
                    to={`/agents/${workflowPermanentId}/${workflowRun.retried_from_workflow_run_id}/overview`}
                  >
                    Retried from {workflowRun.retried_from_workflow_run_id}
                  </Link>
                )}
                {workflowRun.retried_by_workflow_run_id && (
                  <Link
                    className={siblingRunLinkClassName}
                    to={`/agents/${workflowPermanentId}/${workflowRun.retried_by_workflow_run_id}/overview`}
                  >
                    Retried by {workflowRun.retried_by_workflow_run_id}
                  </Link>
                )}
              </div>
            )}
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
      {WorkflowRunMilestoneCard &&
      workflowRun &&
      !workflowRunIsPlaceholder &&
      workflowRun.status === Status.Completed ? (
        <WorkflowRunMilestoneCard
          workflowRunId={workflowRun.workflow_run_id}
          rerun={
            !isEmbedded &&
            !isTaskv2Run &&
            !isWorkflowDeleted &&
            workflowPermanentId
              ? {
                  to: `/agents/${workflowPermanentId}/run`,
                  state: getRerunNavigationState(workflowRun),
                }
              : undefined
          }
        />
      ) : null}
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
                          <ArtifactDownloadLink
                            href={url}
                            className="underline underline-offset-4"
                          >
                            <span>{filename}</span>
                          </ArtifactDownloadLink>
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
              visible={!statusUnavailable && !isFinalized}
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
          workflowRunId={workflowRunId}
          activeItem={selection}
          activeIteration={activeIteration}
          timeline={workflowRunTimeline ?? []}
          timelineReady={workflowRunTimeline !== undefined}
          onSetActiveItem={handleSetActiveItem}
          onSetActiveIteration={handleSetActiveIteration}
          onViewScreenshot={handleViewScreenshot}
        />
      </div>
    </div>
  );
}

export { WorkflowRun };
