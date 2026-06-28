import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Share1Icon } from "@radix-ui/react-icons";
import { usePostHog } from "posthog-js/react";
import { useSearchParams } from "react-router-dom";

import { Status } from "@/api/types";
import { ApiWebhookActionsMenu } from "@/components/ApiWebhookActionsMenu";
import { WebhookReplayDialog } from "@/components/WebhookReplayDialog";
import { useApiCredential } from "@/hooks/useApiCredential";
import { statusIsFinalized } from "@/routes/tasks/types";
import { useRunViewStore } from "@/store/RunViewStore";
import { useStudioShellStore } from "@/store/StudioShellStore";
import { type ApiCommandOptions } from "@/util/apiCommands";
import { runsApiBaseUrl } from "@/util/env";
import { cn } from "@/util/utils";

import { useWorkflowRunTimelineQuery } from "../../hooks/useWorkflowRunTimelineQuery";
import { useWorkflowRunWithWorkflowQuery } from "../../hooks/useWorkflowRunWithWorkflowQuery";
import { WorkflowRunBlockDetail } from "../../workflowRun/WorkflowRunBlockDetail";
import { WorkflowRunTimeline } from "../../workflowRun/WorkflowRunTimeline";
import { WorkflowRunVerificationCodeForm } from "../../workflowRun/WorkflowRunVerificationCodeForm";
import { pickDownloadedFileFilename } from "../../workflowRun/blockDownloadedFiles";
import { getRecordingUrls } from "../../workflowRun/recordingUrls";
import { findActiveItem } from "../../workflowRun/workflowTimelineUtils";
import { getOrderedRunParameters } from "../../utils";
import {
  buildFilmstrip,
  formatElapsed,
  runOutcomeFromStatus,
} from "../runProjections";
import { RunDetailsButton } from "./RunDetailsButton";
import { RunHero } from "./RunHero";
import { RunInputsButton, type RunInputMeta } from "./RunInputsButton";
import { RunOutputsButton, type RunOutputFile } from "./RunOutputsButton";

type RunViewProps = {
  workflowRunId?: string;
  onFix?: () => void;
  onRetry?: () => void;
};

/**
 * Fused run view: browser hero on the left, run timeline tree + block detail on
 * the right, sharing one pinned item via RunViewStore.
 */
export function RunView({ workflowRunId, onFix, onRetry }: RunViewProps) {
  const queryOptions = workflowRunId ? { workflowRunId } : undefined;
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery(queryOptions);
  const { data: timeline } = useWorkflowRunTimelineQuery(queryOptions);
  const pinnedFrameId = useRunViewStore((s) => s.pinnedFrameId);
  const pinFrame = useRunViewStore((s) => s.pinFrame);
  const resetRunView = useRunViewStore((s) => s.reset);
  const studioTab = useStudioShellStore((s) => s.tab);
  const [searchParams, setSearchParams] = useSearchParams();
  const searchParamsRef = useRef(searchParams);
  searchParamsRef.current = searchParams;
  const postHog = usePostHog();
  const apiCredential = useApiCredential();
  const [activeIteration, setActiveIteration] = useState<number | null>(null);
  const [replayOpen, setReplayOpen] = useState(false);

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

  // Pin the resolved run into ?wr= while the Run tab shows it (an ?active=-only link
  // becomes stable). Gated on the tab: RunView also mounts under Editor, no ?wr= there.
  useEffect(() => {
    if (studioTab !== "run") {
      return;
    }
    const resolvedRunId = workflowRun?.workflow_run_id;
    if (!resolvedRunId) {
      return;
    }
    setSearchParams(
      (prev) => {
        if (prev.get("wr") === resolvedRunId && !prev.has("bl")) {
          return prev;
        }
        const next = new URLSearchParams(prev);
        next.set("wr", resolvedRunId);
        // Viewing the run, not the block-run browser — drop stale ?bl= so a reload
        // doesn't snap back to the Browser tab.
        next.delete("bl");
        return next;
      },
      { replace: true },
    );
  }, [studioTab, workflowRun, setSearchParams]);

  const frames = useMemo(() => buildFilmstrip(timeline), [timeline]);

  const outcome = runOutcomeFromStatus(workflowRun?.status);
  const running = outcome === "running";
  // A user-canceled run isn't a failure — don't show the "run failed" CTA.
  const canceled = workflowRun?.status === Status.Canceled;
  const failed = outcome === "failed" && !canceled;

  const lastFrame = frames.length > 0 ? frames[frames.length - 1] : null;
  const shownFrame =
    (pinnedFrameId ? frames.find((f) => f.id === pinnedFrameId) : undefined) ??
    lastFrame ??
    null;

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

  const hasOutputs =
    (extractedInformation != null &&
      Object.values(extractedInformation).some((value) => value !== null)) ||
    downloadedFiles.length > 0;

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

  if (!workflowRun) {
    return (
      <div className="flex h-full w-full items-center justify-center p-8 text-center text-sm text-muted-foreground">
        Run the workflow to watch it live here.
      </div>
    );
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
            shownFrame={shownFrame}
            running={running}
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
            details={
              <RunDetailsButton
                workflowRunId={workflowRun.workflow_run_id}
                status={workflowRun.status}
                startedAt={workflowRun.started_at ?? null}
                finishedAt={workflowRun.finished_at ?? null}
                failureReason={workflowRun.failure_reason ?? null}
                failureCategory={workflowRun.failure_category ?? null}
                browserSessionId={workflowRun.browser_session_id ?? null}
                browserProfileId={workflowRun.browser_profile_id ?? null}
              />
            }
            inputs={
              <RunInputsButton
                parameters={runInputs.parameters}
                meta={runInputs.meta}
              />
            }
            outputs={
              hasOutputs ? (
                <RunOutputsButton
                  workflowRunId={workflowRun.workflow_run_id}
                  workflowTitle={workflowRun.workflow?.title}
                  extractedInformation={extractedInformation}
                  files={downloadedFiles}
                />
              ) : undefined
            }
            actions={
              <>
                <ApiWebhookActionsMenu
                  trigger={
                    <button
                      type="button"
                      className={cn(
                        "inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium",
                        "text-muted-foreground hover:bg-slate-elevation3 hover:text-foreground",
                        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                      )}
                    >
                      <Share1Icon className="h-4 w-4" />
                      API & Webhooks
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
            onFix={onFix}
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
