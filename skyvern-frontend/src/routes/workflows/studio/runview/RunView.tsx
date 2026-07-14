import { useEffect, useMemo, useRef, useState } from "react";
import {
  ClockIcon,
  Cross2Icon,
  ExclamationTriangleIcon,
} from "@radix-ui/react-icons";
import { useNavigate, useSearchParams } from "react-router-dom";

import { Status } from "@/api/types";
import { Button } from "@/components/ui/button";
import { statusIsFinalized } from "@/routes/tasks/types";
import { useRunPaneViewStore } from "@/store/useRunPaneViewStore";
import { useRunViewStore } from "@/store/RunViewStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { useWorkflowBlockSearchStore } from "@/store/WorkflowBlockSearchStore";
import { isRecord } from "@/util/utils";

import { useWorkflowRunTimelineQuery } from "../../hooks/useWorkflowRunTimelineQuery";
import { useWorkflowRunWithWorkflowQuery } from "../../hooks/useWorkflowRunWithWorkflowQuery";
import { ResizableTimelineSplit } from "../../workflowRun/ResizableTimelineSplit";
import { WorkflowRunBlockDetail } from "../../workflowRun/WorkflowRunBlockDetail";
import { WorkflowRunCode } from "../../workflowRun/WorkflowRunCode";
import { WorkflowRunTimeline } from "../../workflowRun/WorkflowRunTimeline";
import { WorkflowRunVerificationCodeForm } from "../../workflowRun/WorkflowRunVerificationCodeForm";
import { pickDownloadedFileFilename } from "../../workflowRun/blockDownloadedFiles";
import { findActiveItem } from "../../workflowRun/workflowTimelineUtils";
import { getOrderedRunParameters } from "../../utils";
import {
  buildFilmstrip,
  formatElapsed,
  runHasOutputs,
  runOutcomeFromStatus,
} from "../runProjections";
import { toReadableSearch } from "../panes";
import { useStudioPanes } from "../useStudioPanes";
import { matchFailureTips } from "./failureTips";
import { buildRunFixMessage } from "./runFixMessage";
import { RunInputsSection, type RunInputMeta } from "./RunInputsSection";
import {
  RunOutputsSection,
  type RunOutputError,
  type RunOutputFile,
} from "./RunOutputsSection";
import { RunPlaceholder } from "./RunPlaceholder";
import { RunSummaryStrip } from "./RunSummaryStrip";
import { resolveTimelineBlockJumpNodeId } from "./timelineBlockJump";

type RunViewProps = {
  workflowRunId?: string;
  // The caller is still resolving which run to show; keep the placeholder in its
  // loading state rather than flashing the "no run yet" empty state.
  runIdPending?: boolean;
  onFix?: (seedMessage?: string) => void;
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
  const queryOptions = workflowRunId ? { workflowRunId } : undefined;
  // isLoading here, not isPending like RunTab: this query is enabled only once a run
  // id exists, so a disabled query means "no run" → fall through to the empty CTA.
  const { data: workflowRun, isLoading } =
    useWorkflowRunWithWorkflowQuery(queryOptions);
  const { data: timeline } = useWorkflowRunTimelineQuery(queryOptions);
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
  const [outputSummary, setOutputSummary] = useState<string | null>(null);

  // A pinned frame belongs to one run; drop it when the run changes, then re-seed
  // from ?active= to restore a deep-linked selection.
  useEffect(() => {
    resetRunView();
    setOutputSummary(null);
    resetPaneView();
    setFailureDismissed(false);
    const active = searchParamsRef.current.get("active");
    if (active) {
      pinFrame(active);
    }
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
    if (new URLSearchParams(window.location.search).get("wr")) {
      return;
    }
    const next = new URLSearchParams(
      window.location.search || searchParamsRef.current.toString(),
    );
    if (next.get("wr")) {
      return;
    }
    next.set("wr", workflowRunId);
    navigate({ search: toReadableSearch(next) }, { replace: true });
  }, [runPaneOpen, workflowRunId, navigate]);

  const frames = useMemo(() => buildFilmstrip(timeline), [timeline]);
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
    if (params.get("wr") !== workflowRunId || params.get("active")) {
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
  }, [workflowRunId, workflowRun, timeline, frames, pinFrame]);

  const outcome = runOutcomeFromStatus(workflowRun?.status);
  // A user-canceled run isn't a failure — don't show the "run failed" CTA.
  const canceled = workflowRun?.status === Status.Canceled;
  const failed = outcome === "failed" && !canceled;
  const provisioning =
    workflowRun?.status === Status.Created ||
    workflowRun?.status === Status.Queued;

  const finalized = workflowRun ? statusIsFinalized(workflowRun) : false;
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

  const fixSeedMessage = useMemo(
    () => buildRunFixMessage(workflowRun?.failure_reason ?? null),
    [workflowRun?.failure_reason],
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
    const runParameters =
      (workflowRun?.parameters as Record<string, unknown> | undefined) ?? {};
    const parameters = getOrderedRunParameters(
      definitionParameters,
      runParameters,
    );
    const meta: RunInputMeta[] = [];
    const pushMeta = (label: string, value: unknown) => {
      if (value === null || value === undefined || value === "") {
        return;
      }
      meta.push({
        label,
        value: typeof value === "string" ? value : JSON.stringify(value),
      });
    };
    pushMeta("Webhook URL", workflowRun?.webhook_callback_url);
    pushMeta("Proxy", workflowRun?.proxy_location);
    pushMeta("Extra HTTP headers", workflowRun?.extra_http_headers);
    pushMeta("Browser session", workflowRun?.browser_session_id);
    pushMeta("Run with", workflowRun?.run_with);
    pushMeta("Max screenshot scrolls", workflowRun?.max_screenshot_scrolls);
    return { parameters, meta };
  }, [workflowRun]);

  // Task 2.0 runs carry their output (and any webhook failure) on task_v2,
  // not on the workflow-run outputs field.
  const observerOutput = workflowRun?.task_v2?.output ?? null;
  const webhookFailureReason =
    workflowRun?.task_v2?.webhook_failure_reason ??
    workflowRun?.webhook_failure_reason ??
    null;

  const hasInputs =
    runInputs.parameters.length > 0 || runInputs.meta.length > 0;
  const hasOutputs = runHasOutputs(workflowRun);

  if (!workflowRun) {
    return <RunPlaceholder loading={isLoading || runIdPending} />;
  }

  const elapsed = formatElapsed(
    workflowRun.started_at ?? workflowRun.created_at ?? null,
    finalized ? (workflowRun.finished_at ?? null) : null,
  );

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-hidden p-3">
        <WorkflowRunVerificationCodeForm
          workflowRunId={workflowRun.workflow_run_id}
        />

        {provisioning ? (
          <div className="flex shrink-0 items-center gap-2 rounded-md border border-border bg-slate-elevation2 px-3 py-1.5 text-xs text-muted-foreground">
            <ClockIcon className="h-3.5 w-3.5 shrink-0" />
            <span>Run queued — waiting to start</span>
          </div>
        ) : null}

        {failed && !failureDismissed ? (
          <div className="shrink-0 rounded-lg border border-destructive/40 bg-slate-elevation1 p-4">
            <div className="flex items-start gap-2 text-sm font-semibold text-foreground">
              <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
              <span className="min-w-0 flex-1">
                {workflowRun.failure_reason ?? "The run failed."}
                {matchFailureTips(workflowRun.failure_reason ?? null).map(
                  (tip) => (
                    <span
                      key={tip}
                      className="mt-1.5 block text-xs font-normal italic text-muted-foreground"
                    >
                      {tip}
                    </span>
                  ),
                )}
              </span>
              <button
                type="button"
                onClick={() => setFailureDismissed(true)}
                className="-mr-1 -mt-1 shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                aria-label="Dismiss"
                title="Dismiss"
              >
                <Cross2Icon className="h-4 w-4" />
              </button>
            </div>
            {onFix || onRetry ? (
              <div className="mt-3 flex flex-wrap gap-2">
                {onFix ? (
                  <Button size="sm" onClick={() => onFix(fixSeedMessage)}>
                    Fix with Copilot
                  </Button>
                ) : null}
                {onRetry ? (
                  <Button size="sm" variant="secondary" onClick={onRetry}>
                    Retry as-is
                  </Button>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}

        {view === "timeline" ? (
          <div className="flex min-h-0 flex-1 flex-col gap-3">
            <RunSummaryStrip workflowRun={workflowRun} elapsed={elapsed} />
            <ResizableTimelineSplit
              className="flex-1"
              top={
                <div className="min-h-0 overflow-hidden">
                  <WorkflowRunTimeline
                    workflowRunId={workflowRunId}
                    hideLiveBadge
                    activeItem={activeItem}
                    activeIteration={activeIteration}
                    onActionItemSelected={(item) => {
                      pinFrame(item.action.action_id);
                    }}
                    onBlockItemSelected={(block) => {
                      pinFrame(block.workflow_run_block_id);
                      const handle =
                        useWorkflowBlockSearchStore.getState().handle;
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
                    }}
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
                <div className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-slate-elevation1">
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
          <div className="min-h-0 flex-1 overflow-y-auto rounded-lg border border-border bg-slate-elevation1 p-4">
            {hasInputs ? (
              <RunInputsSection
                parameters={runInputs.parameters}
                meta={runInputs.meta}
              />
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                No inputs for this run
              </div>
            )}
          </div>
        ) : view === "outputs" ? (
          <div className="min-h-0 flex-1 overflow-y-auto rounded-lg border border-border bg-slate-elevation1 p-4">
            {hasOutputs ? (
              <RunOutputsSection
                workflowRunId={workflowRun.workflow_run_id}
                workflowTitle={workflowRun.workflow?.title}
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
                No outputs for this run
              </div>
            )}
          </div>
        ) : (
          <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-border bg-slate-elevation1 p-2">
            <WorkflowRunCode
              workflowRunId={workflowRun.workflow_run_id}
              showCacheKeyValueSelector
            />
          </div>
        )}
      </div>
    </div>
  );
}
