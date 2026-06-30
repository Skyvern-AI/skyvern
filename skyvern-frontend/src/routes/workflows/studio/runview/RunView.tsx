import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Share1Icon } from "@radix-ui/react-icons";
import { usePostHog } from "posthog-js/react";
import { useParams, useSearchParams } from "react-router-dom";

import { Status } from "@/api/types";
import { ApiWebhookActionsMenu } from "@/components/ApiWebhookActionsMenu";
import { WebhookReplayDialog } from "@/components/WebhookReplayDialog";
import { useApiCredential } from "@/hooks/useApiCredential";
import { statusIsFinalized } from "@/routes/tasks/types";
import {
  isAction,
  isObserverThought,
  isWorkflowRunBlock,
} from "@/routes/workflows/types/workflowRunTypes";
import { useRunViewStore } from "@/store/RunViewStore";
import { useStudioShellStore } from "@/store/StudioShellStore";
import { type ApiCommandOptions } from "@/util/apiCommands";
import { runsApiBaseUrl } from "@/util/env";
import { cn } from "@/util/utils";

import { useDebugSessionQuery } from "../../hooks/useDebugSessionQuery";
import { useWorkflowRunTimelineQuery } from "../../hooks/useWorkflowRunTimelineQuery";
import { useWorkflowRunWithWorkflowQuery } from "../../hooks/useWorkflowRunWithWorkflowQuery";
import { WorkflowRunBlockDetail } from "../../workflowRun/WorkflowRunBlockDetail";
import { WorkflowRunTimeline } from "../../workflowRun/WorkflowRunTimeline";
import { WorkflowRunVerificationCodeForm } from "../../workflowRun/WorkflowRunVerificationCodeForm";
import { pickDownloadedFileFilename } from "../../workflowRun/blockDownloadedFiles";
import { getRecordingUrls } from "../../workflowRun/recordingUrls";
import {
  findActiveItem,
  findTimelineBlock,
  resolveScreenshotBlockId,
} from "../../workflowRun/workflowTimelineUtils";
import { getOrderedRunParameters } from "../../utils";
import {
  actionLabel,
  buildFilmstrip,
  formatElapsed,
  runOutcomeFromStatus,
} from "../runProjections";
import { RunHero } from "./RunHero";
import { type HeroSelection } from "./HeroScreenshot";
import { buildRunFixMessage } from "./runFixMessage";
import { RunInputsSection, type RunInputMeta } from "./RunInputsSection";
import { RunOutputsSection, type RunOutputFile } from "./RunOutputsSection";
import { RunOverviewButton } from "./RunOverviewButton";
import { RunPlaceholder } from "./RunPlaceholder";

type RunViewProps = {
  workflowRunId?: string;
  // The caller is still resolving which run to show; keep the placeholder in its
  // loading state rather than flashing the "no run yet" empty state.
  runIdPending?: boolean;
  onFix?: (seedMessage?: string) => void;
  onRetry?: () => void;
};

/**
 * Fused run view: browser hero on the left, run timeline tree + block detail on
 * the right, sharing one pinned item via RunViewStore.
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
  const { data: debugSession } = useDebugSessionQuery({
    workflowPermanentId,
    enabled: false,
  });
  const pinnedFrameId = useRunViewStore((s) => s.pinnedFrameId);
  const pinFrame = useRunViewStore((s) => s.pinFrame);
  const resetRunView = useRunViewStore((s) => s.reset);
  const headerCompact = useRunViewStore((s) => s.headerCompact);
  const studioTab = useStudioShellStore((s) => s.tab);
  const [searchParams, setSearchParams] = useSearchParams();
  const searchParamsRef = useRef(searchParams);
  searchParamsRef.current = searchParams;
  const postHog = usePostHog();
  const apiCredential = useApiCredential();
  const [activeIteration, setActiveIteration] = useState<number | null>(null);
  const [replayOpen, setReplayOpen] = useState(false);
  const [outputSummary, setOutputSummary] = useState<string | null>(null);

  const recordingUrls = useMemo(
    () => getRecordingUrls(workflowRun),
    [workflowRun],
  );
  const onRecordingPlay = useCallback(
    (index: number) => {
      if (!workflowRun) {
        return;
      }
      postHog.capture("run.recording.viewed", {
        org_id: workflowRun.workflow?.organization_id,
        run_id: workflowRun.workflow_run_id,
        recording_index: index,
        recording_count: recordingUrls.length,
      });
    },
    [postHog, workflowRun, recordingUrls.length],
  );

  // A pinned frame belongs to one run; drop it when the run changes, then re-seed
  // from ?active= to restore a deep-linked selection.
  useEffect(() => {
    resetRunView();
    setOutputSummary(null);
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
        const desired =
          pinnedFrameId && !/:\d+$/.test(pinnedFrameId) ? pinnedFrameId : null;
        if ((prev.get("active") ?? null) === desired) {
          return prev;
        }
        const next = new URLSearchParams(prev);
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
  // the tab: RunView also mounts under Editor, no ?wr= there.
  //
  // The guard reads the LIVE URL, not this render's searchParams: a block-run launch
  // navigates to ?wr=&bl= via a separate router update, and the editor→run transition
  // can fire this effect from a render whose searchParams closure predates it. Reading
  // the live URL avoids writing the stale latest-run id back over the new run (which
  // reverted ?wr= and dropped ?bl=, disabling the debug stream).
  useEffect(() => {
    if (studioTab !== "run") {
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
        if (prev.get("wr")) {
          return prev;
        }
        const next = new URLSearchParams(prev);
        next.set("wr", workflowRunId);
        return next;
      },
      { replace: true },
    );
  }, [studioTab, workflowRunId, setSearchParams]);

  const frames = useMemo(() => buildFilmstrip(timeline), [timeline]);

  const outcome = runOutcomeFromStatus(workflowRun?.status);
  const running = outcome === "running";
  // A user-canceled run isn't a failure — don't show the "run failed" CTA.
  const canceled = workflowRun?.status === Status.Canceled;
  const failed = outcome === "failed" && !canceled;
  // A block run executes in the debug session; on the Run tab show that same live
  // debug stream (the shared node) instead of a separate run stream — but only when
  // the run's browser session IS the current debug session. A historical block-run
  // link whose debug session is gone/different falls back to the normal run view.
  const showDebugStream =
    studioTab === "run" &&
    searchParams.has("bl") &&
    workflowRun?.browser_session_id != null &&
    workflowRun.browser_session_id === debugSession?.browser_session_id;
  const fixSeedMessage = useMemo(
    () => buildRunFixMessage(workflowRun?.failure_reason ?? null),
    [workflowRun?.failure_reason],
  );

  const lastFrame = frames.length > 0 ? frames[frames.length - 1] : null;

  const finalized = workflowRun ? statusIsFinalized(workflowRun) : false;
  const finallyBlockLabel =
    workflowRun?.workflow?.workflow_definition?.finally_block_label ?? null;
  const selectedId =
    pinnedFrameId ?? (running ? "stream" : lastFrame?.id) ?? null;
  const activeItem = useMemo(
    () =>
      findActiveItem(timeline ?? [], selectedId, finalized, finallyBlockLabel),
    [timeline, selectedId, finalized, finallyBlockLabel],
  );

  // Mirror the legacy run page: an action shows its own screenshot, a block shows
  // its representative screenshot (a loop/conditional container resolves to its leaf).
  const heroSelection = useMemo<HeroSelection | null>(() => {
    if (isAction(activeItem)) {
      return {
        kind: "action",
        artifactId: activeItem.screenshot_artifact_id ?? null,
        stepId: activeItem.step_id ?? null,
        actionOrder: activeItem.action_order ?? null,
      };
    }
    if (isWorkflowRunBlock(activeItem)) {
      const screenshotBlockId = resolveScreenshotBlockId(
        timeline ?? [],
        activeItem,
        activeIteration,
      );
      const blockType =
        findTimelineBlock(timeline ?? [], screenshotBlockId)?.block_type ??
        activeItem.block_type ??
        null;
      return {
        kind: "block",
        workflowRunBlockId: screenshotBlockId,
        blockType,
      };
    }
    if (isObserverThought(activeItem)) {
      return { kind: "thought", thoughtId: activeItem.thought_id };
    }
    return null;
  }, [activeItem, timeline, activeIteration]);

  const heroLabel = isAction(activeItem)
    ? actionLabel(activeItem)
    : isWorkflowRunBlock(activeItem)
      ? (activeItem.label ?? "Screenshot")
      : isObserverThought(activeItem)
        ? (activeItem.thought ?? "Thought")
        : "Screenshot";

  const extractedInformation = useMemo<Record<string, unknown> | null>(() => {
    const outputs = workflowRun?.outputs;
    return typeof outputs === "object" &&
      outputs !== null &&
      "extracted_information" in outputs
      ? (outputs.extracted_information as Record<string, unknown>)
      : null;
  }, [workflowRun]);

  const downloadedFiles = useMemo<RunOutputFile[]>(() => {
    const urls = workflowRun?.downloaded_file_urls ?? [];
    const filenameByUrl = new Map<string, string>();
    for (const file of workflowRun?.downloaded_files ?? []) {
      if (file.filename) {
        filenameByUrl.set(file.url, file.filename);
      }
    }
    return urls.map((url) => ({
      url,
      filename: pickDownloadedFileFilename(url, filenameByUrl),
    }));
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

  const hasInputs =
    runInputs.parameters.length > 0 || runInputs.meta.length > 0;
  const hasOutputs =
    (extractedInformation != null &&
      Object.values(extractedInformation).some((value) => value !== null)) ||
    downloadedFiles.length > 0;

  if (!workflowRun) {
    return <RunPlaceholder loading={isLoading || runIdPending} />;
  }

  const elapsed = formatElapsed(
    workflowRun.started_at ?? workflowRun.created_at ?? null,
    finalized ? (workflowRun.finished_at ?? null) : null,
  );

  return (
    <div className="flex h-full min-h-0 w-full flex-col p-3">
      <div className="flex min-h-0 flex-1 items-stretch gap-3 overflow-hidden">
        <div className="flex min-w-0 flex-1 flex-col gap-3 overflow-hidden">
          <WorkflowRunVerificationCodeForm
            workflowRunId={workflowRun.workflow_run_id}
          />
          <RunHero
            workflowRunId={workflowRun.workflow_run_id}
            heroSelection={heroSelection}
            heroLabel={heroLabel}
            running={running}
            showDebugStream={showDebugStream}
            provisioning={
              workflowRun.status === Status.Created ||
              workflowRun.status === Status.Queued
            }
            isPaused={workflowRun.status === Status.Paused}
            failed={failed}
            failureReason={workflowRun.failure_reason ?? null}
            browserSessionId={workflowRun.browser_session_id ?? null}
            recordingUrls={recordingUrls}
            elapsed={elapsed}
            overview={
              <RunOverviewButton
                status={workflowRun.status}
                elapsed={elapsed}
                startedAt={workflowRun.started_at ?? null}
                finishedAt={workflowRun.finished_at ?? null}
                failureReason={workflowRun.failure_reason ?? null}
                failureCategory={workflowRun.failure_category ?? null}
                workflowRunId={workflowRun.workflow_run_id}
                browserSessionId={workflowRun.browser_session_id ?? null}
                browserProfileId={workflowRun.browser_profile_id ?? null}
              />
            }
            inputs={
              hasInputs ? (
                <RunInputsSection
                  parameters={runInputs.parameters}
                  meta={runInputs.meta}
                />
              ) : undefined
            }
            outputs={
              hasOutputs ? (
                <RunOutputsSection
                  workflowRunId={workflowRun.workflow_run_id}
                  workflowTitle={workflowRun.workflow?.title}
                  extractedInformation={extractedInformation}
                  files={downloadedFiles}
                  summary={outputSummary}
                  onSummary={setOutputSummary}
                />
              ) : undefined
            }
            actions={
              <>
                <ApiWebhookActionsMenu
                  trigger={
                    <button
                      type="button"
                      title={headerCompact ? "API & Webhooks" : undefined}
                      aria-label="API & Webhooks"
                      className={cn(
                        "inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium",
                        "text-muted-foreground hover:bg-slate-elevation3 hover:text-foreground",
                        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                      )}
                    >
                      <Share1Icon className="h-4 w-4" />
                      {headerCompact ? null : "API & Webhooks"}
                    </button>
                  }
                  getOptions={() => {
                    const headers: Record<string, string> = {
                      "Content-Type": "application/json",
                      "x-api-key": apiCredential ?? "<your-api-key>",
                    };
                    const body: Record<string, unknown> = {
                      workflow_id: workflowRun.workflow?.workflow_permanent_id,
                      parameters: workflowRun.parameters,
                      proxy_location: workflowRun.proxy_location,
                    };
                    if (workflowRun.max_screenshot_scrolls != null) {
                      body.max_screenshot_scrolls =
                        workflowRun.max_screenshot_scrolls;
                    }
                    if (workflowRun.webhook_callback_url) {
                      body.webhook_url = workflowRun.webhook_callback_url;
                    }
                    return {
                      method: "POST",
                      url: `${runsApiBaseUrl}/run/workflows`,
                      body,
                      headers,
                    } satisfies ApiCommandOptions;
                  }}
                  webhookDisabled={!finalized}
                  onTestWebhook={() => setReplayOpen(true)}
                />
                <WebhookReplayDialog
                  runId={workflowRun.workflow_run_id}
                  disabled={!finalized}
                  open={replayOpen}
                  onOpenChange={setReplayOpen}
                  hideTrigger
                />
              </>
            }
            onRecordingPlay={onRecordingPlay}
            onFix={onFix ? () => onFix(fixSeedMessage) : undefined}
            onRetry={onRetry}
          />
        </div>
        <div className="relative w-[26rem] shrink-0">
          <div className="absolute inset-0 grid grid-rows-[minmax(0,1fr)_minmax(0,1fr)] gap-3">
            <div className="min-h-0 overflow-hidden">
              <WorkflowRunTimeline
                workflowRunId={workflowRunId}
                hideLiveBadge
                activeItem={activeItem}
                activeIteration={activeIteration}
                onActionItemSelected={(item) => pinFrame(item.action.action_id)}
                onBlockItemSelected={(block) =>
                  pinFrame(block.workflow_run_block_id)
                }
                onThoughtItemSelected={(thought) =>
                  pinFrame(thought.thought_id)
                }
                onLiveStreamSelected={() => pinFrame("stream")}
                onIterationSelected={(loopBlock, iterationIndex) => {
                  setActiveIteration(iterationIndex);
                  pinFrame(loopBlock.workflow_run_block_id);
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
                onActionSelect={(payload) => pinFrame(payload.action.action_id)}
                onThoughtSelect={(thought) => pinFrame(thought.thought_id)}
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
