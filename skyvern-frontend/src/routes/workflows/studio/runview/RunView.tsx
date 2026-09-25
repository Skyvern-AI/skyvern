import {
  getRunAttempt,
  runIsLogicallyFinal,
  runIsRetryWaiting,
} from "@/routes/workflows/workflowRun/runRetryState";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { MagnifyingGlassIcon } from "@radix-ui/react-icons";
import {
  Link,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";

import { Status } from "@/api/types";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";

import {
  SELECTED_BLOCK_SEARCH_PARAM,
  SYSTEM_BLOCK_FOCUS_PARAM,
} from "@/routes/workflows/editor/hooks/useSelectedBlockUrlSync";
import {
  type RunPaneView,
  useRunPaneViewStore,
} from "@/store/useRunPaneViewStore";
import { useRunViewStore } from "@/store/RunViewStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { useWorkflowBlockSearchStore } from "@/store/WorkflowBlockSearchStore";
import {
  type WorkflowRunMilestoneCardProps,
  usePageSlots,
} from "@/store/PageSlots";
import { isRecord } from "@/util/utils";
import {
  FirstRunRecoveryGuidance,
  shouldShowRecoveryGuidance,
} from "@/components/onboarding/FirstRunRecoveryGuidance";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { RunTagsEditor } from "@/routes/tasks/components/tagging/RunTagsEditor";
import { useOnboardingStateOptional } from "@/store/onboarding/useOnboardingState";
import {
  getRecoveryGuidanceRetryNavigation,
  RecoveryGuidanceTelemetry,
  retryRunHasStarted,
  type RecoveryGuidanceTelemetryContext,
} from "@/util/onboarding/recoveryGuidanceTelemetry";

import { constructCacheKeyValue } from "../../editor/utils";
import { useBlockScriptsQuery } from "../../hooks/useBlockScriptsQuery";
import { useFallbackEpisodesQuery } from "../../hooks/useFallbackEpisodesQuery";
import { useRefreshOnboardingOnRunCompletion } from "../../hooks/useRefreshOnboardingOnRunCompletion";
import { useWorkflowRunTimelineQuery } from "../../hooks/useWorkflowRunTimelineQuery";
import { useWorkflowRunWithWorkflowQuery } from "../../hooks/useWorkflowRunWithWorkflowQuery";
import { ResizableTimelineSplit } from "../../workflowRun/ResizableTimelineSplit";
import { WorkflowRunBlockDetail } from "../../workflowRun/WorkflowRunBlockDetail";
import { WorkflowRunCode } from "../../workflowRun/WorkflowRunCode";
import { ScriptUpdateCard } from "../../workflowRun/ScriptUpdateCard";
import { WorkflowRunTimeline } from "../../workflowRun/WorkflowRunTimeline";
import { WorkflowRunVerificationCodeForm } from "../../workflowRun/WorkflowRunVerificationCodeForm";
import { findRunCodeBlockFailure } from "../../workflowRun/codeBlockFailure";
import { pickDownloadedFileFilename } from "../../workflowRun/blockDownloadedFiles";
import {
  buildBlockOrderIndex,
  collectTimelineSearchTargets,
  filterTimelineToAttempt,
  findActiveItem,
  flattenTimelineChronologically,
  parseActiveIterationParam,
  type TimelineSearchTarget,
} from "../../workflowRun/workflowTimelineUtils";
import { getOrderedRunParameters, getRerunNavigationState } from "../../utils";
import {
  buildFilmstrip,
  ELAPSED_NEVER_STARTED,
  formatElapsed,
  resolveLandingSelectionId,
  runHasOutputs,
  runOutcomeFromStatus,
} from "../runProjections";
import {
  searchWithRunReference,
  SYSTEM_RUN_FOCUS_PARAM,
  toReadableSearch,
} from "../panes";
import { useStudioPanes } from "../useStudioPanes";
import { collectBlockPrompts } from "./blockPrompts";
import { formatFailureReason } from "../../workflowRun/failureReasonFormat";
import { matchFailureTips } from "./failureTips";
import { RunInputsSection, type RunInputMeta } from "./RunInputsSection";
import {
  RunOutputsSection,
  type RunOutputError,
  type RunOutputFile,
} from "./RunOutputsSection";
import { failingBlock } from "./failingBlock";
import { RunPlaceholder } from "./RunPlaceholder";
import { RunFeedback } from "@/components/feedback/RunFeedback";
import { RunSummaryStrip } from "./RunSummaryStrip";
import { type WorkflowRunBlock } from "../../types/workflowRunTypes";
import {
  FailureRecoveryActions,
  RunFailureLine,
} from "./RunFailurePresentation";
import { resolveEditorSelectionPin } from "./editorSelectionPin";
import { resolveTimelineBlockJumpNodeId } from "./timelineBlockJump";

const RECOVERY_GUIDANCE_TREATMENT_SURFACE_FLAG =
  "RECOVERY_GUIDANCE_TREATMENT_SURFACE";

function isRunPaneView(value: string | null): value is RunPaneView {
  return (
    value === "timeline" ||
    value === "inputs" ||
    value === "outputs" ||
    value === "code"
  );
}

type RunViewProps = {
  workflowRunId?: string;
  // The caller is still resolving which run to show; keep the placeholder in its
  // loading state rather than flashing the "no run yet" empty state.
  runIdPending?: boolean;
  onFix?: (failingLabel?: string | null) => void;
  onRetry?: () => void;
  milestoneRerun?: WorkflowRunMilestoneCardProps["rerun"];
};

function isRunOutputError(value: unknown): value is RunOutputError {
  return isRecord(value);
}

function normalizeRunOutputErrors(value: unknown): RunOutputError[] {
  if (Array.isArray(value)) {
    return value.filter(isRunOutputError);
  }
  return [];
}

// The URL already says what to show. Every automatic landing decision defers to
// it, so the pane and the URL cannot end up naming different things. A pin in
// the store is not part of this: the auto-pin effect below writes one itself, so
// only that effect checks for a pre-existing pin it must not stomp.
function hasExplicitSelection(params: URLSearchParams): boolean {
  return (
    params.has(SYSTEM_RUN_FOCUS_PARAM) ||
    Boolean(params.get("active")) ||
    params.has("bl")
  );
}

// Elapsed is derived from Date.now() during render, and nothing re-renders this
// pane on a schedule — so a live run's clock only advanced when a poll happened
// to return changed data, and visibly froze whenever it did not.
function useLiveClock(active: boolean) {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!active) {
      return;
    }
    const id = window.setInterval(() => setTick((tick) => tick + 1), 1000);
    return () => window.clearInterval(id);
  }, [active]);
}

function TimelineBlockSearch({
  targets,
  onJump,
}: {
  targets: Array<TimelineSearchTarget>;
  onJump: (target: TimelineSearchTarget) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const closeAndReset = () => {
    setOpen(false);
    setQuery("");
  };
  return (
    <Popover
      open={open}
      onOpenChange={(next) => (next ? setOpen(true) : closeAndReset())}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label="Search blocks"
          className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          <MagnifyingGlassIcon className="size-3.5" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" sideOffset={6} className="w-64 p-0">
        <Command
          onKeyDown={(event) => {
            // Keep Escape local: Studio may mount the editor canvas beside the
            // run view, whose window Escape handler would clear its selection.
            if (event.key === "Escape") {
              event.stopPropagation();
              closeAndReset();
            }
          }}
        >
          <CommandInput
            placeholder="Search blocks…"
            value={query}
            onValueChange={setQuery}
          />
          <CommandList>
            <CommandEmpty>No blocks found.</CommandEmpty>
            <CommandGroup>
              {targets.map((target) => (
                <CommandItem
                  key={target.block.workflow_run_block_id}
                  value={target.block.workflow_run_block_id}
                  keywords={[target.label]}
                  onSelect={() => {
                    onJump(target);
                    closeAndReset();
                  }}
                >
                  {target.order !== null ? (
                    <span className="mr-2 shrink-0 text-muted-foreground">
                      #{target.order}
                    </span>
                  ) : null}
                  <span className="truncate">{target.label}</span>
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

/**
 * Overview pane body: the run timeline + step detail (under the summary strip),
 * with Inputs / Outputs / Code as sibling views. The view toggles live in the
 * pane header (RunPaneViewToggles) and share useRunPaneViewStore. Visuals
 * (live stream, screenshots, recordings) live in the Browser pane, which
 * follows this pane's selection via RunViewStore and ?active=.
 */
export function RunView({
  workflowRunId,
  runIdPending = false,
  onFix,
  onRetry,
  milestoneRerun,
}: RunViewProps) {
  const { workflowRunMilestoneCard: WorkflowRunMilestoneCard } = usePageSlots();
  const { runId: pathRunId } = useParams();
  const queryOptions = { workflowRunId };
  // isLoading here, not isPending like RunTab: this query is enabled only once a run
  // id exists, so a disabled query means "no run" → fall through to the empty CTA.
  const {
    data: workflowRun,
    isLoading,
    isPlaceholderData: runIsPlaceholder,
    isError: statusUnavailable,
  } = useWorkflowRunWithWorkflowQuery(queryOptions);
  useRefreshOnboardingOnRunCompletion(
    runIsPlaceholder ? undefined : workflowRun,
  );
  const { data: retainedTimeline, isPlaceholderData: timelineIsPlaceholder } =
    useWorkflowRunTimelineQuery(queryOptions);
  // The timeline payload carries no run id of its own, so keepPreviousData serves
  // the previous run's timeline on both a switch and a clear.
  const timeline =
    !workflowRunId || timelineIsPlaceholder ? undefined : retainedTimeline;
  const currentTimeline = useMemo(
    () =>
      timeline
        ? filterTimelineToAttempt(
            timeline,
            workflowRun?.attempts ?? [],
            getRunAttempt(workflowRun ?? {}),
          )
        : undefined,
    [timeline, workflowRun],
  );
  const pinnedFrameId = useRunViewStore((s) => s.pinnedFrameId);
  const activeIteration = useRunViewStore((s) => s.activeIteration);
  const pinFrame = useRunViewStore((s) => s.pinFrame);
  const jumpToLive = useRunViewStore((s) => s.jumpToLive);
  const resetRunView = useRunViewStore((s) => s.reset);
  const {
    panes: studioPanes,
    openPane,
    setOpenPanes,
    preserveNextEntry,
  } = useStudioPanes();
  const runPaneOpen = studioPanes.includes("overview");
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const embedded = searchParams.get("embed") === "true";
  const requestedView = searchParams.get("view");
  const requestedActive = searchParams.get("active");
  const requestedIteration = requestedActive
    ? parseActiveIterationParam(searchParams.get("iteration"))
    : null;
  const searchParamsRef = useRef(searchParams);
  searchParamsRef.current = searchParams;
  const view = useRunPaneViewStore((s) => s.view);
  const setPaneView = useRunPaneViewStore((s) => s.setView);
  const resetPaneView = useRunPaneViewStore((s) => s.reset);
  const [outputSummary, setOutputSummary] = useState<string | null>(null);
  const finalized =
    !statusUnavailable && workflowRun
      ? runIsLogicallyFinal(workflowRun)
      : false;
  const workflow = workflowRun?.workflow;
  const workflowPermanentId = workflow?.workflow_permanent_id;
  const isWorkflowDeleted = Boolean(workflow?.deleted_at);
  const cacheKey = workflow?.cache_key ?? "";
  const cacheKeyValue =
    constructCacheKeyValue({ codeKey: cacheKey, workflow, workflowRun }) ?? "";
  const { data: blockScriptsPublished } = useBlockScriptsQuery({
    cacheKey,
    cacheKeyValue,
    enabled: finalized && !isWorkflowDeleted,
    workflowPermanentId,
    status: "published",
    workflowRunId: workflowRun?.workflow_run_id,
  });
  const { data: fallbackEpisodes } = useFallbackEpisodesQuery({
    workflowPermanentId,
    workflowRunId: workflowRun?.workflow_run_id,
    enabled: finalized && !isWorkflowDeleted,
  });
  const onboarding = useOnboardingStateOptional();
  const recoveryGuidanceTreatmentSurfaceEnabled =
    useFeatureFlag(RECOVERY_GUIDANCE_TREATMENT_SURFACE_FLAG) === true;
  const recoveryGuidanceAssignment =
    onboarding?.recoveryGuidanceAssignment ?? null;
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
  const showFirstFailedRunRecovery = shouldShowRecoveryGuidance({
    assignment: recoveryGuidanceAssignment,
    workflowRunId: workflowRun?.workflow_run_id,
    treatmentSurfaceEnabled: recoveryGuidanceTreatmentSurfaceEnabled,
  });
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

  const handleFirstFailedRunRetry = useCallback(() => {
    if (
      !workflowRun ||
      !workflowPermanentId ||
      !recoveryGuidanceTelemetryContext
    ) {
      return;
    }
    navigate(`/agents/${workflowPermanentId}/run`, {
      state: {
        ...getRerunNavigationState(workflowRun),
        recoveryGuidanceRetry: recoveryGuidanceTelemetryContext,
      },
    });
  }, [
    navigate,
    recoveryGuidanceTelemetryContext,
    workflowPermanentId,
    workflowRun,
  ]);

  // Last editor selection the canvas→run sync below acted on.
  const syncedBlockLabelRef = useRef<string | null>(null);

  // A pinned frame belongs to one run; drop it when the run changes.
  useEffect(() => {
    resetRunView();
    setOutputSummary(null);
    resetPaneView();
    // Adopt the standing editor selection without acting on it, so a cold open
    // and a run switch land via ?active= / the auto-pin one-shot rather than
    // racing them; the sync applies from the next selection change onward.
    syncedBlockLabelRef.current = searchParamsRef.current.get(
      SELECTED_BLOCK_SEARCH_PARAM,
    );
  }, [workflowRunId, resetRunView, resetPaneView]);

  // Rehydrate deep-linked selections on cold load, run switches, and browser
  // history changes that update ?active= / ?iteration= for the same run.
  const lastHydratedSelectionRef = useRef<{
    runId: string | undefined;
    active: string | null;
    iteration: number | null;
  } | null>(null);
  useEffect(() => {
    const previous = lastHydratedSelectionRef.current;
    if (
      previous !== null &&
      previous.runId === workflowRunId &&
      previous.active === requestedActive &&
      previous.iteration === requestedIteration
    ) {
      return;
    }
    lastHydratedSelectionRef.current = {
      runId: workflowRunId,
      active: requestedActive,
      iteration: requestedIteration,
    };

    const current = useRunViewStore.getState();
    if (requestedActive) {
      if (
        previous?.runId !== workflowRunId ||
        current.pinnedFrameId !== requestedActive ||
        current.activeIteration !== requestedIteration
      ) {
        pinFrame(requestedActive, requestedIteration);
      }
    } else if (
      previous !== null &&
      previous.runId === workflowRunId &&
      previous.active !== null &&
      (current.pinnedFrameId !== null || current.activeIteration !== null)
    ) {
      jumpToLive();
    }
  }, [
    jumpToLive,
    pinFrame,
    requestedActive,
    requestedIteration,
    workflowRunId,
  ]);

  useEffect(() => {
    if (isRunPaneView(requestedView)) {
      setPaneView(requestedView);
    } else if (requestedView !== "recording") {
      resetPaneView();
    }
  }, [requestedView, resetPaneView, setPaneView, workflowRunId]);

  // Mirror the pinned item to ?active= so selection survives reload. Skip the first
  // pass after a run change so the seed above doesn't fight the URL.
  const lastMirroredRunRef = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (lastMirroredRunRef.current !== workflowRunId) {
      lastMirroredRunRef.current = workflowRunId;
      return;
    }
    // Build on the LIVE URL (the ref can be one render stale): a concurrent
    // navigation (block-run launch, pane toggle) is already visible there.
    const next = new URLSearchParams(
      window.location.search || searchParamsRef.current.toString(),
    );
    const desired =
      pinnedFrameId && !/:\d+$/.test(pinnedFrameId) ? pinnedFrameId : null;
    const desiredIteration =
      desired && activeIteration !== null ? String(activeIteration) : null;
    if (
      (next.get("active") ?? null) === desired &&
      (next.get("iteration") ?? null) === desiredIteration
    ) {
      return;
    }
    if (desired) {
      next.set("active", desired);
    } else {
      next.delete("active");
    }
    if (desiredIteration) {
      next.set("iteration", desiredIteration);
    } else {
      next.delete("iteration");
    }
    const search = toReadableSearch(next);
    preserveNextEntry(search);
    navigate({ search }, { replace: true });
  }, [
    activeIteration,
    pinnedFrameId,
    workflowRunId,
    navigate,
    preserveNextEntry,
  ]);

  // Stabilize an ?active=-only deep link by ADDING ?wr= when it's absent. Gated on
  // the Overview pane being open: RunView stays mounted while its pane is closed.
  //
  // The guard reads the LIVE URL, not this render's searchParams: a block-run launch
  // navigates to ?wr=&bl= via a separate router update, and this effect can fire from
  // a render whose searchParams closure predates it. Reading the live URL avoids
  // writing the stale latest-run id back over the new run (which reverted ?wr= and
  // dropped ?bl=, disabling the debug stream).
  useEffect(() => {
    if (!runPaneOpen) {
      return;
    }
    if (!workflowRunId) {
      return;
    }
    // Under the short /runs/{wr} URL the run is already named by the path, so
    // pinning ?wr= would only duplicate it; the pane reads the run from either.
    if (pathRunId === workflowRunId) {
      return;
    }
    if (new URLSearchParams(window.location.search).get("wr")) {
      return;
    }
    const live =
      window.location.search || searchParamsRef.current.toString() || "";
    if (new URLSearchParams(live).get("wr")) {
      return;
    }
    const search = searchWithRunReference(live, workflowRunId);
    preserveNextEntry(search);
    navigate({ search }, { replace: true });
  }, [runPaneOpen, workflowRunId, pathRunId, navigate, preserveNextEntry]);

  const frames = useMemo(
    () => buildFilmstrip(currentTimeline),
    [currentTimeline],
  );
  const searchTargets = useMemo(
    () =>
      currentTimeline
        ? collectTimelineSearchTargets(
            flattenTimelineChronologically(currentTimeline),
            buildBlockOrderIndex(currentTimeline),
          )
        : [],
    [currentTimeline],
  );

  const outcome = runOutcomeFromStatus(workflowRun);
  // A user-canceled run isn't a failure — don't show the "run failed" CTA.
  const canceled = workflowRun?.status === Status.Canceled;
  // While a run switch is still serving the previous payload, this status belongs to the old run
  // but the id in scope is the new one — acting on the pair posts a run that has not failed.
  const failed =
    !statusUnavailable &&
    !runIsPlaceholder &&
    outcome === "failed" &&
    !canceled;
  useLiveClock(Boolean(workflowRun) && !finalized && !statusUnavailable);
  const finallyBlockLabel =
    workflowRun?.workflow?.workflow_definition?.finally_block_label ?? null;
  const landingSelectionId = useMemo(
    () => resolveLandingSelectionId(frames, currentTimeline, finalized),
    [frames, currentTimeline, finalized],
  );
  const codeFailure = useMemo(
    () =>
      findRunCodeBlockFailure(
        workflowRun?.failure_reason,
        currentTimeline,
        finallyBlockLabel,
      ),
    [workflowRun?.failure_reason, currentTimeline, finallyBlockLabel],
  );
  const failedBlock = useMemo(
    () => failingBlock(currentTimeline, finallyBlockLabel),
    [currentTimeline, finallyBlockLabel],
  );
  const failureBlockId =
    codeFailure?.workflowRunBlockId ??
    failedBlock?.workflow_run_block_id ??
    null;

  // Landing the selection on the LAST timeline item — so the Browser pane
  // shows the final screenshot instead of an idle replay — happens on two
  // paths sharing this one-shot: cold-opening a deep link to an already-
  // finished run (?wr= with no ?active=), and a run watched live to its
  // running→terminal transition. Explicit choices always win (?active=, a
  // user's timeline pin, or — for the live-watch path — a pinned view pill),
  // and ?bl= block-iterate links keep their live debug surface.
  const autoPinDecidedForRunRef = useRef<string | undefined>(undefined);
  const watchedLiveRunRef = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!workflowRunId || autoPinDecidedForRunRef.current === workflowRunId) {
      return;
    }
    if (!workflowRun || !timeline) {
      return;
    }
    // On a run switch, keepPreviousData briefly serves the PREVIOUS run's
    // (finalized) run + timeline. Deciding auto-pin on it would lock THIS run's
    // one-shot to the old run's last frame and never re-decide; wait for the
    // new run's real payload.
    if (runIsPlaceholder || timelineIsPlaceholder) {
      return;
    }
    if (!runIsLogicallyFinal(workflowRun)) {
      // Still running: leave the one-shot open so the terminal transition of
      // a watched run lands the same last-item pin as a cold open.
      watchedLiveRunRef.current = workflowRunId;
      return;
    }
    autoPinDecidedForRunRef.current = workflowRunId;
    const watchedLive = watchedLiveRunRef.current === workflowRunId;
    const params = new URLSearchParams(
      window.location.search || searchParamsRef.current.toString(),
    );
    if (hasExplicitSelection(params)) {
      return;
    }
    // The short /runs/{wr} URL names the run in the path rather than ?wr=, so a
    // matching path id is the focused deep link too (parity with ?wr= cold open).
    const isFocusedDeepLink =
      params.get("wr") === workflowRunId || pathRunId === workflowRunId;
    // The normal Studio route resolves the latest run without naming it in the
    // URL. For failures, give its strip the same zero-click failed-block target.
    const isLatestRunRoute = !params.get("wr") && !pathRunId;
    if (!isFocusedDeepLink && !(failed && isLatestRunRoute)) {
      return;
    }
    if (useRunViewStore.getState().pinnedFrameId) {
      return;
    }
    // A view pill pinned mid-watch is an explicit choice; the ?active= write
    // that follows a pin would hand the pane back to the machine and override
    // it. (Cold opens skip this guard: a run swap resets the pill to auto in
    // useBrowserPaneView, possibly in this same effect flush.)
    if (watchedLive && useStudioBrowserStore.getState().view !== "auto") {
      return;
    }
    // A failed run lands on the block that killed it, so its Failure section
    // is on screen with zero clicks; anything else lands on the run's final
    // state so the Browser pane shows the final screenshot.
    const target =
      failed && failureBlockId ? failureBlockId : landingSelectionId;
    if (target) {
      pinFrame(target);
    }
  }, [
    workflowRunId,
    workflowRun,
    timeline,
    landingSelectionId,
    failed,
    failureBlockId,
    pinFrame,
    pathRunId,
    runIsPlaceholder,
    timelineIsPlaceholder,
  ]);

  // A run that had already succeeded when it was opened lands on its Outputs;
  // a failed one keeps the timeline, where its failure section and Fix/Retry live.
  // Explicit choices win here for the same reason they do for the pin above:
  // a deep link names what to show, and switching the pane hides it.
  const outputsLandingDecidedForRunRef = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (
      !workflowRunId ||
      outputsLandingDecidedForRunRef.current === workflowRunId ||
      !workflowRun ||
      runIsPlaceholder ||
      timelineIsPlaceholder
    ) {
      return;
    }
    outputsLandingDecidedForRunRef.current = workflowRunId;
    if (!finalized || outcome !== "success") {
      return;
    }
    const landingSearchParams = new URLSearchParams(
      window.location.search || searchParamsRef.current.toString(),
    );
    if (
      landingSearchParams.has("view") ||
      hasExplicitSelection(landingSearchParams)
    ) {
      return;
    }
    if (
      runHasOutputs(workflowRun) &&
      useRunPaneViewStore.getState().view === "timeline"
    ) {
      setPaneView("outputs");
    }
  }, [
    workflowRunId,
    workflowRun,
    finalized,
    outcome,
    runIsPlaceholder,
    timelineIsPlaceholder,
    setPaneView,
  ]);

  // This pane never hosts the live stream, so a "stream" pin (or no pin) follows
  // the live edge — the same resolution the Browser pane applies in useRunVisuals.
  const selectedId =
    pinnedFrameId && pinnedFrameId !== "stream"
      ? pinnedFrameId
      : landingSelectionId;
  const activeItem = useMemo(
    () =>
      findActiveItem(
        (selectedId ? timeline : currentTimeline) ?? [],
        selectedId,
        finalized,
        finallyBlockLabel,
      ),
    [timeline, currentTimeline, selectedId, finalized, finallyBlockLabel],
  );

  // Selecting a block on the editor canvas moves the run selection onto it, so
  // this pane's detail and the Browser pane's screenshot (which follows the
  // ?active= this pin mirrors to) both land on that block — the reverse of the
  // timeline→canvas jump in onBlockItemSelected. Gated on the label CHANGING:
  // re-resolving on every timeline poll would drag the pin off an action the
  // user picked in a different block.
  const selectedBlockLabel = searchParams.get(SELECTED_BLOCK_SEARCH_PARAM);
  useEffect(() => {
    // Nothing to resolve against yet; leave the label unadopted so it applies
    // once the timeline arrives.
    if (!timeline) {
      return;
    }
    if (selectedBlockLabel === syncedBlockLabelRef.current) {
      return;
    }
    syncedBlockLabelRef.current = selectedBlockLabel;
    const selection = {
      editorOpen: studioPanes.includes("editor"),
      runPaneOpen,
      finalized,
      blockRun: searchParamsRef.current.has("bl"),
      timeline,
      selectedBlockLabel,
      systemFocusLabel: searchParamsRef.current.get(SYSTEM_BLOCK_FOCUS_PARAM),
      pinnedFrameId: useRunViewStore.getState().pinnedFrameId,
    };
    // An explicit historical ID wins over the current attempt's same-label block.
    if (resolveEditorSelectionPin(selection) === null) return;
    const blockId = resolveEditorSelectionPin({
      ...selection,
      timeline: currentTimeline,
    });
    if (blockId) {
      pinFrame(blockId);
    }
  }, [
    selectedBlockLabel,
    timeline,
    currentTimeline,
    studioPanes,
    runPaneOpen,
    finalized,
    pinFrame,
  ]);

  const extractedInformation = useMemo<Record<string, unknown> | null>(() => {
    const outputs = workflowRun?.outputs;
    return isRecord(outputs) && "extracted_information" in outputs
      ? (outputs.extracted_information as Record<string, unknown>)
      : null;
  }, [workflowRun]);

  const downloadedFiles = useMemo<RunOutputFile[]>(() => {
    const filenameByUrl = new Map<string, string>();
    const files: RunOutputFile[] = [];
    const seen = new Set<string>();
    const pushFile = (url: string, filename?: string | null) => {
      if (seen.has(url)) {
        return;
      }
      seen.add(url);
      files.push({
        url,
        filename: filename || pickDownloadedFileFilename(url, filenameByUrl),
      });
    };
    for (const file of workflowRun?.downloaded_files ?? []) {
      if (file.filename) {
        filenameByUrl.set(file.url, file.filename);
      }
      pushFile(file.url, file.filename);
    }
    // Prefer rich metadata first; URL fallback only fills gaps without duplicating.
    for (const url of workflowRun?.downloaded_file_urls ?? []) {
      pushFile(url);
    }
    return files;
  }, [workflowRun]);

  const runErrors = useMemo<RunOutputError[]>(() => {
    return normalizeRunOutputErrors(workflowRun?.errors);
  }, [workflowRun]);

  const runInputs = useMemo(() => {
    const definitionParameters =
      workflowRun?.workflow?.workflow_definition?.parameters;
    const blockPrompts = collectBlockPrompts(
      workflowRun?.workflow?.workflow_definition?.blocks ?? [],
    );
    const runParameters =
      (workflowRun?.parameters as Record<string, unknown> | undefined) ?? {};
    const parameters = getOrderedRunParameters(
      definitionParameters,
      runParameters,
    );
    const meta: RunInputMeta[] = [];
    const pushMeta = (
      label: string,
      value: unknown,
      href?: (value: string) => string,
    ) => {
      if (value === null || value === undefined || value === "") {
        return;
      }
      const text = typeof value === "string" ? value : JSON.stringify(value);
      meta.push({ label, value: text, to: href?.(text) });
    };
    pushMeta("Webhook URL", workflowRun?.webhook_callback_url);
    // Task 2.0 runs store TOTP config on task_v2, not the top-level run.
    pushMeta(
      "TOTP URL",
      workflowRun?.totp_verification_url ??
        workflowRun?.task_v2?.totp_verification_url,
    );
    pushMeta(
      "TOTP identifier",
      workflowRun?.totp_identifier ?? workflowRun?.task_v2?.totp_identifier,
    );
    pushMeta("Proxy", workflowRun?.proxy_location);
    pushMeta("Extra HTTP headers", workflowRun?.extra_http_headers);
    pushMeta(
      "Browser session",
      workflowRun?.browser_session_id,
      (id) => `/browser-session/${id}/stream`,
    );
    pushMeta(
      "Browser profile",
      workflowRun?.browser_profile_id,
      (id) => `/browser-profiles/${id}`,
    );
    pushMeta("Run with", workflowRun?.run_with);
    pushMeta("Max screenshot scrolls", workflowRun?.max_screenshot_scrolls);
    return { parameters, blockPrompts, meta };
  }, [workflowRun]);

  // Task 2.0 runs carry their output (and any webhook failure) on task_v2,
  // not on the workflow-run outputs field.
  const observerOutput = workflowRun?.task_v2?.output ?? null;
  const webhookFailureReason =
    workflowRun?.task_v2?.webhook_failure_reason ??
    workflowRun?.webhook_failure_reason ??
    null;

  const hasInputs =
    runInputs.parameters.length > 0 ||
    runInputs.blockPrompts.length > 0 ||
    runInputs.meta.length > 0;
  const hasOutputs = runHasOutputs(workflowRun);

  if (!workflowRun) {
    return (
      <RunPlaceholder
        unavailable={statusUnavailable}
        loading={
          isLoading ||
          runIdPending ||
          (Boolean(workflowRunId) && runIsPlaceholder)
        }
      />
    );
  }

  // Same rule as the summary strip: created_at is always set, so falling back to
  // it shows a queued run an elapsed time it never accrued. The never-started
  // sentinel is dropped so the timeline omits the value entirely rather than
  // rendering a bare dash.
  const elapsedValue = formatElapsed(
    workflowRun.started_at ?? null,
    finalized ? (workflowRun.finished_at ?? null) : null,
  );
  // Once finalized the strip reads "Ran for …" off the run's own endpoints;
  // only a live run needs the ticking value.
  const liveElapsed =
    finalized || elapsedValue === ELAPSED_NEVER_STARTED ? null : elapsedValue;
  // When the editor is open and the label is unique on the canvas, focus that
  // block's node. Shared by the block row, its action rows, and the search.
  const focusCanvasBlock = (block: WorkflowRunBlock) => {
    const handle = useWorkflowBlockSearchStore.getState().handle;
    if (!handle) {
      return;
    }
    const nodeId = resolveTimelineBlockJumpNodeId({
      editorOpen: studioPanes.includes("editor"),
      targets: handle.getTargets(),
      label: block.label,
    });
    if (nodeId) {
      handle.focusBlock(nodeId);
    }
  };
  const selectTimelineBlock = (block: WorkflowRunBlock) => {
    pinFrame(block.workflow_run_block_id);
    focusCanvasBlock(block);
  };
  const failureReason = formatFailureReason(
    workflowRun.failure_reason ?? "The run failed.",
  );
  const failureHeadline = codeFailure
    ? codeFailure.title
    : failureReason.headline;
  const failureDetail = failureReason.detail;
  const failureTips = matchFailureTips(workflowRun.failure_reason ?? null);
  // Editing code cannot reach a sandbox that was never available, so a fault the
  // block did not cause offers a retry alone rather than a copilot session that
  // would rewrite working code.
  const showFix = codeFailure === null || codeFailure.recovery !== "retry";
  const hasFixAction = Boolean(onFix && showFix);
  const recovery =
    failed && !embedded ? (
      <>
        <FailureRecoveryActions
          onFix={
            onFix && hasFixAction
              ? () => onFix(failedBlock?.label ?? null)
              : undefined
          }
          onRetry={onRetry}
        />
        {onRetry &&
        showFirstFailedRunRecovery &&
        recoveryGuidanceTelemetryContext ? (
          <FirstRunRecoveryGuidance
            telemetryContext={recoveryGuidanceTelemetryContext}
            workflowPermanentId={workflowPermanentId}
            onRetry={handleFirstFailedRunRetry}
          />
        ) : null}
      </>
    ) : null;
  const jumpToFailedBlock = failedBlock
    ? () => selectTimelineBlock(failedBlock)
    : undefined;

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col gap-2 overflow-hidden p-2">
      <WorkflowRunVerificationCodeForm
        workflowRunId={workflowRun.workflow_run_id}
      />
      {embedded ? null : (
        <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1">
          <RunTagsEditor workflowRunId={workflowRun.workflow_run_id} />
          {workflowRun.retried_from_workflow_run_id ? (
            <Link
              className="text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
              to={`/runs/${workflowRun.retried_from_workflow_run_id}`}
            >
              Retried from {workflowRun.retried_from_workflow_run_id}
            </Link>
          ) : null}
          {workflowRun.retried_by_workflow_run_id ? (
            <Link
              className="text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
              to={`/runs/${workflowRun.retried_by_workflow_run_id}`}
            >
              Retried by {workflowRun.retried_by_workflow_run_id}
            </Link>
          ) : null}
        </div>
      )}
      {WorkflowRunMilestoneCard &&
      runPaneOpen &&
      !runIsPlaceholder &&
      runIsLogicallyFinal(workflowRun) &&
      workflowRun.status === Status.Completed ? (
        <WorkflowRunMilestoneCard
          workflowRunId={workflowRun.workflow_run_id}
          rerun={embedded ? undefined : milestoneRerun}
        />
      ) : null}
      {fallbackEpisodes && fallbackEpisodes.episodes.length > 0 ? (
        <ScriptUpdateCard
          episodes={fallbackEpisodes.episodes}
          scriptId={blockScriptsPublished?.script_id}
        />
      ) : null}

      {view === "timeline" ? (
        <div className="flex min-h-0 flex-1 flex-col gap-2">
          <RunSummaryStrip
            workflowRun={workflowRun}
            timeline={currentTimeline}
            liveElapsed={statusUnavailable ? null : liveElapsed}
            statusUnavailable={statusUnavailable}
            trailing={
              <TimelineBlockSearch
                targets={searchTargets}
                onJump={(target) => selectTimelineBlock(target.block)}
              />
            }
          />
          {!statusUnavailable &&
          !runIsPlaceholder &&
          !canceled &&
          runIsLogicallyFinal(workflowRun) ? (
            <RunFeedback
              targetType="workflow_run"
              targetId={workflowRun.workflow_run_id}
              variant={failed ? "report" : "thumbs"}
            />
          ) : null}
          {failed || runIsRetryWaiting(workflowRun) ? (
            <RunFailureLine
              workflowRun={workflowRun}
              blockLabel={failedBlock?.label ?? null}
              headline={failureHeadline}
              detail={failureDetail}
              onJump={jumpToFailedBlock}
              tips={failureTips}
            >
              {recovery}
            </RunFailureLine>
          ) : null}
          <ResizableTimelineSplit
            className="flex-1"
            top={
              <div className="min-h-0 overflow-hidden">
                <WorkflowRunTimeline
                  workflowRunId={workflowRunId}
                  hideBorder
                  hideHeader
                  activeItem={activeItem}
                  activeIteration={activeIteration}
                  onActionItemSelected={(item) => {
                    // Pin first: the canvas→run sync sees a pin already inside
                    // this block and leaves it on the action instead of
                    // bouncing back to the block header.
                    pinFrame(item.action.action_id);
                    focusCanvasBlock(item.block);
                  }}
                  onBlockItemSelected={selectTimelineBlock}
                  onThoughtItemSelected={(thought) => {
                    pinFrame(thought.thought_id);
                  }}
                  onLiveStreamSelected={() => {
                    pinFrame("stream");
                  }}
                  onIterationSelected={(loopBlock, iterationIndex) => {
                    pinFrame(loopBlock.workflow_run_block_id, iterationIndex);
                  }}
                />
              </div>
            }
            bottom={
              <div className="flex min-h-0 flex-col overflow-hidden border-t border-border">
                <WorkflowRunBlockDetail
                  activeItem={activeItem}
                  activeIteration={activeIteration}
                  timeline={
                    (pinnedFrameId && pinnedFrameId !== "stream"
                      ? timeline
                      : currentTimeline) ?? []
                  }
                  timelineReady={Boolean(timeline)}
                  showDownloadedFiles
                  workflowRunId={workflowRunId}
                  onThoughtSelect={(thought) => pinFrame(thought.thought_id)}
                  onViewScreenshot={(workflowRunBlockId) => {
                    if (embedded) {
                      setOpenPanes(["browser"]);
                    } else {
                      openPane("browser");
                    }
                    // Let the pane-open navigation commit before mirroring the
                    // pin; otherwise the pin's URL writer can drop `browser`.
                    requestAnimationFrame(() => pinFrame(workflowRunBlockId));
                  }}
                  statedFailureBlockId={failed ? failureBlockId : null}
                  statedFailureHeadline={failed ? failureHeadline : null}
                />
              </div>
            }
          />
        </div>
      ) : view === "inputs" ? (
        <div className="min-h-0 flex-1 overflow-y-auto">
          {hasInputs ? (
            <RunInputsSection
              parameters={runInputs.parameters}
              blockPrompts={runInputs.blockPrompts}
              meta={runInputs.meta}
            />
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              No inputs for this run
            </div>
          )}
        </div>
      ) : view === "outputs" ? (
        <div className="min-h-0 flex-1 overflow-y-auto">
          {hasOutputs ? (
            <RunOutputsSection
              workflowRunId={workflowRun.workflow_run_id}
              workflowTitle={workflowRun.workflow?.title}
              outputs={workflowRun.outputs}
              extractedInformation={extractedInformation}
              files={downloadedFiles}
              errors={runErrors}
              observerOutput={observerOutput}
              webhookFailureReason={webhookFailureReason}
              summary={outputSummary}
              onSummary={setOutputSummary}
            />
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              {/* A run that hasn't finished has no outputs *yet*. Stating the
                  finished fact while it is still working reads as "this run
                  produced nothing". */}
              {finalized
                ? "No output captured for this run"
                : "Outputs appear when the run finishes"}
            </div>
          )}
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <WorkflowRunCode
            workflowRunId={workflowRun.workflow_run_id}
            showCacheKeyValueSelector
          />
        </div>
      )}
    </div>
  );
}
