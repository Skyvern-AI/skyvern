import { useEffect, useMemo, useRef, useState } from "react";
import {
  Cross2Icon,
  ExclamationTriangleIcon,
  MagicWandIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { Status } from "@/api/types";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { statusIsFinalized } from "@/routes/tasks/types";
import {
  SELECTED_BLOCK_SEARCH_PARAM,
  SYSTEM_BLOCK_FOCUS_PARAM,
} from "@/routes/workflows/editor/hooks/useSelectedBlockUrlSync";
import { useRunPaneViewStore } from "@/store/useRunPaneViewStore";
import { useRunViewStore } from "@/store/RunViewStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { useWorkflowBlockSearchStore } from "@/store/WorkflowBlockSearchStore";
import { cn, isRecord } from "@/util/utils";

import { useWorkflowRunTimelineQuery } from "../../hooks/useWorkflowRunTimelineQuery";
import { useWorkflowRunWithWorkflowQuery } from "../../hooks/useWorkflowRunWithWorkflowQuery";
import { ResizableTimelineSplit } from "../../workflowRun/ResizableTimelineSplit";
import { WorkflowRunBlockDetail } from "../../workflowRun/WorkflowRunBlockDetail";
import { WorkflowRunCode } from "../../workflowRun/WorkflowRunCode";
import {
  TimelineBlockSearch,
  WorkflowRunTimeline,
} from "../../workflowRun/WorkflowRunTimeline";
import { WorkflowRunVerificationCodeForm } from "../../workflowRun/WorkflowRunVerificationCodeForm";
import { CodeBlockFailureDetails } from "../../workflowRun/CodeBlockFailureDetails";
import { findRunCodeBlockFailure } from "../../workflowRun/codeBlockFailure";
import { pickDownloadedFileFilename } from "../../workflowRun/blockDownloadedFiles";
import {
  buildBlockOrderIndex,
  collectTimelineSearchTargets,
  findActiveItem,
  flattenTimelineChronologically,
} from "../../workflowRun/workflowTimelineUtils";
import { getOrderedRunParameters } from "../../utils";
import {
  buildFilmstrip,
  ELAPSED_NEVER_STARTED,
  formatElapsed,
  runHasOutputs,
  runOutcomeFromStatus,
} from "../runProjections";
import { searchWithRunReference, toReadableSearch } from "../panes";
import { useStudioPanes } from "../useStudioPanes";
import { collectBlockPrompts } from "./blockPrompts";
import {
  failureDetailIsLong,
  formatFailureReason,
} from "./failureReasonFormat";
import { matchFailureTips } from "./failureTips";
import { buildRunFixMessage } from "./runFixMessage";
import { RunInputsSection, type RunInputMeta } from "./RunInputsSection";
import {
  RunOutputsSection,
  type RunOutputError,
  type RunOutputFile,
} from "./RunOutputsSection";
import { failingBlockLabel } from "./failingBlock";
import { RunPlaceholder } from "./RunPlaceholder";
import { RunSummaryStrip } from "./RunSummaryStrip";
import { type WorkflowRunBlock } from "../../types/workflowRunTypes";
import { resolveEditorSelectionPin } from "./editorSelectionPin";
import { resolveTimelineBlockJumpNodeId } from "./timelineBlockJump";

type RunViewProps = {
  workflowRunId?: string;
  // The caller is still resolving which run to show; keep the placeholder in its
  // loading state rather than flashing the "no run yet" empty state.
  runIdPending?: boolean;
  onFix?: (seedMessage?: string, failingLabel?: string | null) => void;
  onRetry?: () => void;
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
}: RunViewProps) {
  const { runId: pathRunId } = useParams();
  const queryOptions = workflowRunId ? { workflowRunId } : undefined;
  // isLoading here, not isPending like RunTab: this query is enabled only once a run
  // id exists, so a disabled query means "no run" → fall through to the empty CTA.
  const {
    data: workflowRun,
    isLoading,
    isPlaceholderData: runIsPlaceholder,
  } = useWorkflowRunWithWorkflowQuery(queryOptions);
  const { data: timeline, isPlaceholderData: timelineIsPlaceholder } =
    useWorkflowRunTimelineQuery(queryOptions);
  const pinnedFrameId = useRunViewStore((s) => s.pinnedFrameId);
  const activeIteration = useRunViewStore((s) => s.activeIteration);
  const pinFrame = useRunViewStore((s) => s.pinFrame);
  const resetRunView = useRunViewStore((s) => s.reset);
  const { panes: studioPanes } = useStudioPanes();
  const runPaneOpen = studioPanes.includes("overview");
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const searchParamsRef = useRef(searchParams);
  searchParamsRef.current = searchParams;
  const view = useRunPaneViewStore((s) => s.view);
  const resetPaneView = useRunPaneViewStore((s) => s.reset);
  const [failureDismissed, setFailureDismissed] = useState(false);
  const [failureDetailExpanded, setFailureDetailExpanded] = useState(false);
  const [outputSummary, setOutputSummary] = useState<string | null>(null);

  // Last editor selection the canvas→run sync below acted on.
  const syncedBlockLabelRef = useRef<string | null>(null);

  // A pinned frame belongs to one run; drop it when the run changes, then re-seed
  // from ?active= to restore a deep-linked selection.
  useEffect(() => {
    resetRunView();
    setOutputSummary(null);
    resetPaneView();
    setFailureDismissed(false);
    setFailureDetailExpanded(false);
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
    // The short /runs/{wr} URL names the run in the path rather than ?wr=, so a
    // matching path id is the focused deep link too (parity with ?wr= cold open).
    const isFocusedDeepLink =
      params.get("wr") === workflowRunId || pathRunId === workflowRunId;
    if (!isFocusedDeepLink || params.get("active")) {
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
    const last = frames.length > 0 ? frames[frames.length - 1] : null;
    if (last) {
      pinFrame(last.id);
    }
  }, [
    workflowRunId,
    workflowRun,
    timeline,
    frames,
    pinFrame,
    pathRunId,
    runIsPlaceholder,
    timelineIsPlaceholder,
  ]);

  const outcome = runOutcomeFromStatus(workflowRun?.status);
  // A user-canceled run isn't a failure — don't show the "run failed" CTA.
  const canceled = workflowRun?.status === Status.Canceled;
  const failed = outcome === "failed" && !canceled;
  const finalized = workflowRun ? statusIsFinalized(workflowRun) : false;
  useLiveClock(Boolean(workflowRun) && !finalized);
  const finallyBlockLabel =
    workflowRun?.workflow?.workflow_definition?.finally_block_label ?? null;
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

  const fixSeedMessage = useMemo(
    () => buildRunFixMessage(workflowRun?.failure_reason ?? null),
    [workflowRun?.failure_reason],
  );

  const codeFailure = useMemo(
    () =>
      findRunCodeBlockFailure(
        workflowRun?.failure_reason,
        timeline,
        finallyBlockLabel,
      ),
    [workflowRun?.failure_reason, timeline, finallyBlockLabel],
  );

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
    return <RunPlaceholder loading={isLoading || runIdPending} />;
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
  const failureDetailLong = failureReason.detail
    ? failureDetailIsLong(failureReason.detail)
    : false;
  const codeFailureDetail = codeFailure
    ? (failureReason.detail ?? workflowRun.failure_reason?.trim() ?? null)
    : null;
  // Editing code cannot reach a sandbox that was never available, so a fault the
  // block did not cause offers a retry alone rather than a copilot session that
  // would rewrite working code.
  const showFix = codeFailure === null || codeFailure.recovery !== "retry";
  const hasFixAction = Boolean(onFix && showFix);

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col gap-2 overflow-hidden p-2">
      <WorkflowRunVerificationCodeForm
        workflowRunId={workflowRun.workflow_run_id}
      />
      {failed && !failureDismissed && view === "timeline" ? (
        <Alert className="shrink-0 border-destructive/40 bg-destructive/5 py-3.5 dark:bg-destructive/10 [&>svg]:text-destructive">
          <ExclamationTriangleIcon className="h-4 w-4" />
          <div className="min-w-0 pr-6">
            <AlertTitle className="mb-0 text-sm font-semibold leading-5 text-foreground">
              {codeFailure ? codeFailure.title : failureReason.headline}
            </AlertTitle>
            <AlertDescription className="mt-1 text-xs leading-relaxed text-muted-foreground">
              {codeFailure ? <p>{codeFailure.guidance}</p> : null}
              {codeFailure ? (
                <CodeBlockFailureDetails
                  failure={codeFailure}
                  reason={codeFailureDetail}
                />
              ) : failureReason.detail ? (
                <>
                  <p
                    className={cn(
                      "mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-muted-foreground",
                      !failureDetailExpanded && "line-clamp-3",
                    )}
                  >
                    {failureReason.detail}
                  </p>
                  {failureDetailLong ? (
                    <button
                      type="button"
                      onClick={() => setFailureDetailExpanded((v) => !v)}
                      className="mt-1 text-xs font-medium text-muted-foreground underline-offset-2 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    >
                      {failureDetailExpanded ? "Show less" : "Show more"}
                    </button>
                  ) : null}
                </>
              ) : null}
              {matchFailureTips(workflowRun.failure_reason ?? null).map(
                (tip) => (
                  <span
                    key={tip}
                    className="mt-1.5 block text-xs italic text-muted-foreground"
                  >
                    {tip}
                  </span>
                ),
              )}
              {hasFixAction || onRetry ? (
                <div className="mt-3 flex flex-wrap gap-2">
                  {onFix && showFix ? (
                    <Button
                      size="sm"
                      onClick={() =>
                        onFix(
                          fixSeedMessage,
                          failingBlockLabel(timeline, finallyBlockLabel),
                        )
                      }
                    >
                      <MagicWandIcon
                        className="mr-1.5 h-3.5 w-3.5"
                        aria-hidden="true"
                      />
                      Fix with Copilot
                    </Button>
                  ) : null}
                  {onRetry ? (
                    <Button
                      size="sm"
                      variant={hasFixAction ? "secondary" : "default"}
                      onClick={onRetry}
                    >
                      <ReloadIcon
                        className="mr-1.5 h-3.5 w-3.5"
                        aria-hidden="true"
                      />
                      Retry
                    </Button>
                  ) : null}
                </div>
              ) : null}
            </AlertDescription>
            <button
              type="button"
              onClick={() => setFailureDismissed(true)}
              className="absolute right-2 top-2 shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              aria-label="Dismiss"
              title="Dismiss"
            >
              <Cross2Icon className="h-4 w-4" />
            </button>
          </div>
        </Alert>
      ) : null}

      {view === "timeline" ? (
        <div className="flex min-h-0 flex-1 flex-col gap-2">
          <RunSummaryStrip
            workflowRun={workflowRun}
            timeline={timeline}
            liveElapsed={liveElapsed}
            trailing={
              <TimelineBlockSearch
                targets={searchTargets}
                onJump={(target) => selectTimelineBlock(target.block)}
              />
            }
          />
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
