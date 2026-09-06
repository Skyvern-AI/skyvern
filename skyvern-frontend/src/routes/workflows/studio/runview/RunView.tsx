import { useEffect, useMemo, useRef, useState } from "react";
import { MagnifyingGlassIcon } from "@radix-ui/react-icons";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

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
import { statusIsFinalized } from "@/routes/tasks/types";
import {
  SELECTED_BLOCK_SEARCH_PARAM,
  SYSTEM_BLOCK_FOCUS_PARAM,
} from "@/routes/workflows/editor/hooks/useSelectedBlockUrlSync";
import { useRunPaneViewStore } from "@/store/useRunPaneViewStore";
import { useRunViewStore } from "@/store/RunViewStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { useWorkflowBlockSearchStore } from "@/store/WorkflowBlockSearchStore";
import {
  type WorkflowRunMilestoneCardProps,
  usePageSlots,
} from "@/store/PageSlots";
import { isRecord } from "@/util/utils";

import { useWorkflowRunTimelineQuery } from "../../hooks/useWorkflowRunTimelineQuery";
import { useWorkflowRunWithWorkflowQuery } from "../../hooks/useWorkflowRunWithWorkflowQuery";
import { ResizableTimelineSplit } from "../../workflowRun/ResizableTimelineSplit";
import { WorkflowRunBlockDetail } from "../../workflowRun/WorkflowRunBlockDetail";
import { WorkflowRunCode } from "../../workflowRun/WorkflowRunCode";
import { WorkflowRunTimeline } from "../../workflowRun/WorkflowRunTimeline";
import { WorkflowRunVerificationCodeForm } from "../../workflowRun/WorkflowRunVerificationCodeForm";
import { findRunCodeBlockFailure } from "../../workflowRun/codeBlockFailure";
import { pickDownloadedFileFilename } from "../../workflowRun/blockDownloadedFiles";
import {
  buildBlockOrderIndex,
  collectTimelineSearchTargets,
  findActiveItem,
  flattenTimelineChronologically,
  type TimelineSearchTarget,
} from "../../workflowRun/workflowTimelineUtils";
import { getOrderedRunParameters } from "../../utils";
import {
  buildFilmstrip,
  ELAPSED_NEVER_STARTED,
  formatElapsed,
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
import { RunSummaryStrip } from "./RunSummaryStrip";
import { type WorkflowRunBlock } from "../../types/workflowRunTypes";
import {
  FailureRecoveryActions,
  RunFailureLine,
} from "./RunFailurePresentation";
import { resolveEditorSelectionPin } from "./editorSelectionPin";
import { resolveTimelineBlockJumpNodeId } from "./timelineBlockJump";

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
                  value={target.label}
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
  const { data: retainedTimeline, isPlaceholderData: timelineIsPlaceholder } =
    useWorkflowRunTimelineQuery(queryOptions);
  // The timeline payload carries no run id of its own, so keepPreviousData serves
  // the previous run's timeline on both a switch and a clear.
  const timeline =
    !workflowRunId || timelineIsPlaceholder ? undefined : retainedTimeline;
  const pinnedFrameId = useRunViewStore((s) => s.pinnedFrameId);
  const activeIteration = useRunViewStore((s) => s.activeIteration);
  const pinFrame = useRunViewStore((s) => s.pinFrame);
  const resetRunView = useRunViewStore((s) => s.reset);
  const { panes: studioPanes, openPane } = useStudioPanes();
  const runPaneOpen = studioPanes.includes("overview");
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const searchParamsRef = useRef(searchParams);
  searchParamsRef.current = searchParams;
  const view = useRunPaneViewStore((s) => s.view);
  const resetPaneView = useRunPaneViewStore((s) => s.reset);
  const [outputSummary, setOutputSummary] = useState<string | null>(null);

  // Last editor selection the canvas→run sync below acted on.
  const syncedBlockLabelRef = useRef<string | null>(null);

  // A pinned frame belongs to one run; drop it when the run changes, then re-seed
  // from ?active= to restore a deep-linked selection.
  useEffect(() => {
    resetRunView();
    setOutputSummary(null);
    resetPaneView();
    const active = searchParamsRef.current.get("active");
    if (active) {
      pinFrame(active);
    }
    // Adopt the standing editor selection without acting on it, so a cold open
    // and a run switch land via ?active= / the auto-pin one-shot rather than
    // racing them; the sync applies from the next selection change onward.
    syncedBlockLabelRef.current = searchParamsRef.current.get(
      SELECTED_BLOCK_SEARCH_PARAM,
    );
  }, [workflowRunId, resetRunView, resetPaneView, pinFrame]);

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
    if ((next.get("active") ?? null) === desired) {
      return;
    }
    if (desired) {
      next.set("active", desired);
    } else {
      next.delete("active");
    }
    navigate({ search: toReadableSearch(next) }, { replace: true });
  }, [pinnedFrameId, workflowRunId, navigate]);

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
    navigate(
      { search: searchWithRunReference(live, workflowRunId) },
      { replace: true },
    );
  }, [runPaneOpen, workflowRunId, pathRunId, navigate]);

  const frames = useMemo(() => buildFilmstrip(timeline), [timeline]);
  const searchTargets = useMemo(
    () =>
      timeline
        ? collectTimelineSearchTargets(
            flattenTimelineChronologically(timeline),
            buildBlockOrderIndex(timeline),
          )
        : [],
    [timeline],
  );
  const lastFrame = frames.length > 0 ? frames[frames.length - 1] : null;

  const outcome = runOutcomeFromStatus(workflowRun?.status);
  // A user-canceled run isn't a failure — don't show the "run failed" CTA.
  const canceled = workflowRun?.status === Status.Canceled;
  // While a run switch is still serving the previous payload, this status belongs to the old run
  // but the id in scope is the new one — acting on the pair posts a run that has not failed.
  const failed =
    !statusUnavailable &&
    !runIsPlaceholder &&
    outcome === "failed" &&
    !canceled;
  const finalized =
    !statusUnavailable && workflowRun ? statusIsFinalized(workflowRun) : false;
  useLiveClock(Boolean(workflowRun) && !finalized && !statusUnavailable);
  const finallyBlockLabel =
    workflowRun?.workflow?.workflow_definition?.finally_block_label ?? null;
  const codeFailure = useMemo(
    () =>
      findRunCodeBlockFailure(
        workflowRun?.failure_reason,
        timeline,
        finallyBlockLabel,
      ),
    [workflowRun?.failure_reason, timeline, finallyBlockLabel],
  );
  const failedBlock = useMemo(
    () => failingBlock(timeline, finallyBlockLabel),
    [timeline, finallyBlockLabel],
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
    if (!statusIsFinalized(workflowRun)) {
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
    if (params.has(SYSTEM_RUN_FOCUS_PARAM)) {
      return;
    }
    // The short /runs/{wr} URL names the run in the path rather than ?wr=, so a
    // matching path id is the focused deep link too (parity with ?wr= cold open).
    const isFocusedDeepLink =
      params.get("wr") === workflowRunId || pathRunId === workflowRunId;
    // The normal Studio route resolves the latest run without naming it in the
    // URL. For failures, give its strip the same zero-click failed-block target.
    const isLatestRunRoute = !params.get("wr") && !pathRunId;
    if (
      (!isFocusedDeepLink && !(failed && isLatestRunRoute)) ||
      params.get("active")
    ) {
      return;
    }
    if (params.has("bl")) {
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
    // is on screen with zero clicks; anything else lands on the last item so
    // the Browser pane shows the final screenshot.
    const last = frames.length > 0 ? frames[frames.length - 1] : null;
    const target =
      failed && failureBlockId ? failureBlockId : (last?.id ?? null);
    if (target) {
      pinFrame(target);
    }
  }, [
    workflowRunId,
    workflowRun,
    timeline,
    frames,
    failed,
    failureBlockId,
    pinFrame,
    pathRunId,
    runIsPlaceholder,
    timelineIsPlaceholder,
  ]);

  // This pane never hosts the live stream, so a "stream" pin (or no pin) follows
  // the live edge — the same resolution the Browser pane applies in useRunVisuals.
  const selectedId =
    pinnedFrameId && pinnedFrameId !== "stream"
      ? pinnedFrameId
      : (lastFrame?.id ?? null);
  const activeItem = useMemo(
    () =>
      findActiveItem(timeline ?? [], selectedId, finalized, finallyBlockLabel),
    [timeline, selectedId, finalized, finallyBlockLabel],
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
    const blockId = resolveEditorSelectionPin({
      editorOpen: studioPanes.includes("editor"),
      runPaneOpen,
      finalized,
      blockRun: searchParamsRef.current.has("bl"),
      timeline,
      selectedBlockLabel,
      systemFocusLabel: searchParamsRef.current.get(SYSTEM_BLOCK_FOCUS_PARAM),
      pinnedFrameId: useRunViewStore.getState().pinnedFrameId,
    });
    if (blockId) {
      pinFrame(blockId);
    }
  }, [
    selectedBlockLabel,
    timeline,
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
        loading={
          isLoading ||
          runIdPending ||
          statusUnavailable ||
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
  const recovery = failed ? (
    <FailureRecoveryActions
      onFix={
        onFix && hasFixAction
          ? () => onFix(failedBlock?.label ?? null)
          : undefined
      }
      onRetry={onRetry}
    />
  ) : null;
  const jumpToFailedBlock = failedBlock
    ? () => selectTimelineBlock(failedBlock)
    : undefined;

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col gap-2 overflow-hidden p-2">
      <WorkflowRunVerificationCodeForm
        workflowRunId={workflowRun.workflow_run_id}
      />
      {WorkflowRunMilestoneCard &&
      runPaneOpen &&
      !runIsPlaceholder &&
      workflowRun.status === Status.Completed ? (
        <WorkflowRunMilestoneCard
          workflowRunId={workflowRun.workflow_run_id}
          rerun={milestoneRerun}
        />
      ) : null}

      {view === "timeline" ? (
        <div className="flex min-h-0 flex-1 flex-col gap-2">
          <RunSummaryStrip
            workflowRun={workflowRun}
            timeline={timeline}
            liveElapsed={statusUnavailable ? null : liveElapsed}
            statusUnavailable={statusUnavailable}
            trailing={
              <TimelineBlockSearch
                targets={searchTargets}
                onJump={(target) => selectTimelineBlock(target.block)}
              />
            }
          />
          {failed ? (
            <RunFailureLine
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
                  timeline={timeline ?? []}
                  timelineReady={Boolean(timeline)}
                  showDownloadedFiles
                  workflowRunId={workflowRunId}
                  onThoughtSelect={(thought) => pinFrame(thought.thought_id)}
                  onViewScreenshot={(workflowRunBlockId) => {
                    openPane("browser");
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
                ? "No outputs for this run"
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
