import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityLogIcon,
  ClockIcon,
  CodeIcon,
  Cross2Icon,
  ExclamationTriangleIcon,
  FileTextIcon,
  ListBulletIcon,
  ReaderIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useParams, useSearchParams } from "react-router-dom";

import { Status } from "@/api/types";
import { Button } from "@/components/ui/button";
import { statusIsFinalized } from "@/routes/tasks/types";
import { useRunViewStore } from "@/store/RunViewStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { isRecord } from "@/util/utils";

import { useIsGeneratingCode } from "../../editor/hooks/useIsGeneratingCode";
import { constructCacheKeyValue } from "../../editor/utils";
import { useWorkflowRunTimelineQuery } from "../../hooks/useWorkflowRunTimelineQuery";
import { useWorkflowRunWithWorkflowQuery } from "../../hooks/useWorkflowRunWithWorkflowQuery";
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
  runOutcomeFromStatus,
} from "../runProjections";
import { studioPanelId } from "../constants";
import { useStudioPanes } from "../useStudioPanes";
import { ViewToggle } from "../ViewToggle";
import { matchFailureTips } from "./failureTips";
import { buildRunFixMessage } from "./runFixMessage";
import { RunInputsSection, type RunInputMeta } from "./RunInputsSection";
import {
  RunOutputsSection,
  type RunOutputError,
  type RunOutputFile,
} from "./RunOutputsSection";
import { RunOverviewSection } from "./RunOverviewSection";
import { RunPlaceholder } from "./RunPlaceholder";

type RunViewProps = {
  workflowRunId?: string;
  // The caller is still resolving which run to show; keep the placeholder in its
  // loading state rather than flashing the "no run yet" empty state.
  runIdPending?: boolean;
  onFix?: (seedMessage?: string) => void;
  onRetry?: () => void;
};

type RunPaneView = "timeline" | "overview" | "inputs" | "outputs" | "code";

// Below this row width the view-toggle labels collapse to icons — the pane,
// not the viewport, decides (studio panes share the stage side by side).
const TOGGLE_ROW_COMPACT_BELOW_PX = 440;

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
 * Run pane body: the run timeline + step detail, with Overview / Inputs /
 * Outputs / Code as sibling views. Visuals (live stream, screenshots,
 * recordings) live in the Browser pane, which follows this pane's selection
 * via RunViewStore and ?active=.
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
  const { workflowPermanentId } = useParams();
  const cacheKey = workflowRun?.workflow?.cache_key ?? "";
  const codeGenerating = useIsGeneratingCode({
    cacheKey,
    cacheKeyValue: constructCacheKeyValue({
      codeKey: cacheKey,
      workflow: workflowRun?.workflow,
      workflowRun,
    }),
    workflowPermanentId,
    workflowRunId,
  });
  const pinnedFrameId = useRunViewStore((s) => s.pinnedFrameId);
  const activeIteration = useRunViewStore((s) => s.activeIteration);
  const pinFrame = useRunViewStore((s) => s.pinFrame);
  const jumpToLive = useRunViewStore((s) => s.jumpToLive);
  const resetRunView = useRunViewStore((s) => s.reset);
  const setBrowserPaneView = useStudioBrowserStore((s) => s.setView);
  const { panes: studioPanes, openPane } = useStudioPanes();
  const runPaneOpen = studioPanes.includes("run");
  const [searchParams, setSearchParams] = useSearchParams();
  const searchParamsRef = useRef(searchParams);
  searchParamsRef.current = searchParams;
  const [view, setView] = useState<RunPaneView>("timeline");
  const [failureDismissed, setFailureDismissed] = useState(false);
  const [outputSummary, setOutputSummary] = useState<string | null>(null);

  // A pinned frame belongs to one run; drop it when the run changes, then re-seed
  // from ?active= to restore a deep-linked selection.
  useEffect(() => {
    resetRunView();
    setOutputSummary(null);
    setView("timeline");
    setFailureDismissed(false);
    const active = searchParamsRef.current.get("active");
    if (active) {
      pinFrame(active);
    }
  }, [workflowRunId, resetRunView, pinFrame]);

  // Mirror the pinned item to ?active= so selection survives reload. Skip the first
  // pass after a run change so the seed above doesn't fight the URL.
  const lastMirroredRunRef = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (lastMirroredRunRef.current !== workflowRunId) {
      lastMirroredRunRef.current = workflowRunId;
      return;
    }
    setSearchParams(
      (prev) => {
        // Build on the LIVE URL (prev is this render's closure): a concurrent
        // navigation (block-run launch, pane toggle) is already visible there.
        const base = window.location.search || `?${prev.toString()}`;
        const next = new URLSearchParams(base);
        const desired =
          pinnedFrameId && !/:\d+$/.test(pinnedFrameId) ? pinnedFrameId : null;
        if ((next.get("active") ?? null) === desired) {
          return next;
        }
        if (desired) {
          next.set("active", desired);
        } else {
          next.delete("active");
        }
        return next;
      },
      { replace: true },
    );
  }, [pinnedFrameId, workflowRunId, setSearchParams]);

  // Stabilize an ?active=-only deep link by ADDING ?wr= when it's absent. Gated on
  // the Run pane being open: RunView stays mounted while its pane is closed.
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
    setSearchParams(
      (prev) => {
        const base = window.location.search || `?${prev.toString()}`;
        const next = new URLSearchParams(base);
        if (next.get("wr")) {
          return next;
        }
        next.set("wr", workflowRunId);
        return next;
      },
      { replace: true },
    );
  }, [runPaneOpen, workflowRunId, setSearchParams]);

  const frames = useMemo(() => buildFilmstrip(timeline), [timeline]);
  const lastFrame = frames.length > 0 ? frames[frames.length - 1] : null;

  const outcome = runOutcomeFromStatus(workflowRun?.status);
  const running = outcome === "running";
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

  const focusBrowserPane = useCallback(() => {
    // An explicit "watch live": unpin back to the live edge and hand the
    // Browser pane a live intent (it may be sitting on a replay view).
    jumpToLive();
    setBrowserPaneView("live");
    openPane("browser");
    // Defer past the pane-open commit so the scroll sees the visible panel.
    requestAnimationFrame(() => {
      const reduceMotion =
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      document.getElementById(studioPanelId("browser"))?.scrollIntoView({
        behavior: reduceMotion ? "auto" : "smooth",
        block: "nearest",
        inline: "nearest",
      });
    });
  }, [jumpToLive, setBrowserPaneView, openPane]);
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
  const hasOutputs =
    runErrors.length > 0 ||
    (extractedInformation != null &&
      Object.values(extractedInformation).some((value) => value !== null)) ||
    downloadedFiles.length > 0 ||
    observerOutput != null ||
    webhookFailureReason != null;

  // Collapse toggle labels to icons by the row's own width.
  const hasRun = workflowRun != null;
  const toggleRowRef = useRef<HTMLDivElement>(null);
  const [toggleCompact, setToggleCompact] = useState(false);
  useEffect(() => {
    const el = toggleRowRef.current;
    if (!hasRun || !el || typeof ResizeObserver === "undefined") {
      return;
    }
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? 0;
      // A closed pane measures 0 (display:none); keep its last real state.
      if (width > 0) {
        setToggleCompact(width < TOGGLE_ROW_COMPACT_BELOW_PX);
      }
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [hasRun]);

  if (!workflowRun) {
    return <RunPlaceholder loading={isLoading || runIdPending} />;
  }

  const elapsed = formatElapsed(
    workflowRun.started_at ?? workflowRun.created_at ?? null,
    finalized ? (workflowRun.finished_at ?? null) : null,
  );

  // A selected view whose data vanished (run change settles late) falls back
  // to the timeline instead of rendering an empty body.
  const resolvedView =
    (view === "inputs" && !hasInputs) || (view === "outputs" && !hasOutputs)
      ? "timeline"
      : view;

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      <div
        ref={toggleRowRef}
        className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2"
      >
        <div
          role="group"
          aria-label="Run view"
          className="flex min-w-0 shrink items-center gap-0.5 overflow-hidden rounded-md border border-slate-700 bg-slate-elevation2 p-0.5"
        >
          <ViewToggle
            active={resolvedView === "timeline"}
            onClick={() => setView("timeline")}
            compact={toggleCompact}
            label="Timeline"
            icon={<ActivityLogIcon className="h-3 w-3" />}
          />
          <ViewToggle
            active={resolvedView === "overview"}
            onClick={() => setView("overview")}
            compact={toggleCompact}
            label="Overview"
            icon={<ReaderIcon className="h-3 w-3" />}
          />
          {hasInputs ? (
            <ViewToggle
              active={resolvedView === "inputs"}
              onClick={() => setView("inputs")}
              compact={toggleCompact}
              label="Inputs"
              icon={<ListBulletIcon className="h-3 w-3" />}
            />
          ) : null}
          {hasOutputs ? (
            <ViewToggle
              active={resolvedView === "outputs"}
              onClick={() => setView("outputs")}
              compact={toggleCompact}
              label="Outputs"
              icon={<FileTextIcon className="h-3 w-3" />}
            />
          ) : null}
          <ViewToggle
            active={resolvedView === "code"}
            onClick={() => setView("code")}
            compact={toggleCompact}
            label="Code"
            title={
              codeGenerating ? "Generating cached code for this run" : undefined
            }
            icon={
              codeGenerating ? (
                <ReloadIcon
                  data-testid="code-generating-spinner"
                  className="h-3 w-3 animate-spin"
                />
              ) : (
                <CodeIcon className="h-3 w-3" />
              )
            }
          />
        </div>
        <span className="min-w-0 flex-1" />
        {running && !provisioning ? (
          <button
            type="button"
            onClick={focusBrowserPane}
            title="Watch live in the Browser pane"
            className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-border bg-slate-elevation2 px-2.5 py-1 text-[11px] font-medium text-foreground hover:bg-slate-elevation3 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
            Live
          </button>
        ) : null}
      </div>

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
                  <Button
                    size="sm"
                    className="bg-studio-accent text-foreground hover:bg-studio-accent/90"
                    onClick={() => onFix(fixSeedMessage)}
                  >
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

        {resolvedView === "timeline" ? (
          <div className="grid min-h-0 flex-1 grid-rows-[minmax(0,1fr)_minmax(0,1fr)] gap-3">
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
          </div>
        ) : resolvedView === "overview" ? (
          <div className="min-h-0 flex-1 overflow-y-auto rounded-lg border border-border bg-slate-elevation1 p-4">
            <RunOverviewSection
              workflowRun={workflowRun}
              actionCount={frames.length}
              elapsed={elapsed}
            />
          </div>
        ) : resolvedView === "inputs" ? (
          <div className="min-h-0 flex-1 overflow-y-auto rounded-lg border border-border bg-slate-elevation1 p-4">
            <RunInputsSection
              parameters={runInputs.parameters}
              meta={runInputs.meta}
            />
          </div>
        ) : resolvedView === "outputs" ? (
          <div className="min-h-0 flex-1 overflow-y-auto rounded-lg border border-border bg-slate-elevation1 p-4">
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
