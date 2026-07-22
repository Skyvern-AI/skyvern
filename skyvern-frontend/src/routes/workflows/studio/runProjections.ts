import {
  ActionsApiResponse,
  ActionType,
  ReadableActionTypes,
  Status,
  WorkflowRunStatusApiResponseWithWorkflow,
} from "@/api/types";
import { statusIsAFailureType, statusIsFinalized } from "@/routes/tasks/types";
import {
  isBlockItem,
  WorkflowRunTimelineItem,
} from "@/routes/workflows/types/workflowRunTypes";
import { flattenTimelineChronologically } from "@/routes/workflows/workflowRun/workflowTimelineUtils";
import { isRecord } from "@/util/utils";
import { basicLocalTimeFormat, normalizeUtcTimestamp } from "@/util/timeFormat";

export type RunOutcome = "idle" | "running" | "failed" | "success";

// Run timestamps are naive ISO (no Z), so normalize to UTC before diffing — else
// a live run's (now - start) is skewed by the local timezone offset.
export function formatElapsed(
  startIso: string | null,
  endIso: string | null,
): string {
  if (!startIso) {
    return "—";
  }
  const start = new Date(normalizeUtcTimestamp(startIso)).getTime();
  if (Number.isNaN(start)) {
    return "—";
  }
  const endMs = endIso
    ? new Date(normalizeUtcTimestamp(endIso)).getTime()
    : Date.now();
  const end = Number.isNaN(endMs) ? Date.now() : endMs;
  const sec = Math.max(0, Math.round((end - start) / 1000));
  if (sec < 60) {
    return `${sec}s`;
  }
  return `${Math.floor(sec / 60)}m ${sec % 60}s`;
}

// The elapsed value's hover tooltip: the full created/queued/started/finished
// breakdown, one per line, omitting timestamps the run doesn't have yet.
export function formatRunTimesTooltip(
  workflowRun: WorkflowRunStatusApiResponseWithWorkflow,
): string {
  const finalized = statusIsFinalized(workflowRun);
  return [
    workflowRun.created_at
      ? `Created ${basicLocalTimeFormat(workflowRun.created_at)}`
      : null,
    workflowRun.queued_at
      ? `Queued ${basicLocalTimeFormat(workflowRun.queued_at)}`
      : null,
    workflowRun.started_at
      ? `Started ${basicLocalTimeFormat(workflowRun.started_at)}`
      : null,
    finalized && workflowRun.finished_at
      ? `Finished ${basicLocalTimeFormat(workflowRun.finished_at)}`
      : null,
  ]
    .filter(Boolean)
    .join("\n");
}

export function runOutcomeFromStatus(
  status: Status | null | undefined,
): RunOutcome {
  if (!status) {
    return "idle";
  }
  if (status === Status.Completed) {
    return "success";
  }
  if (statusIsAFailureType({ status }) || status === Status.Canceled) {
    return "failed";
  }
  return "running";
}

// Null while the run is in-flight; the tab shows only the terminal status.
export function finalizedRunStatus(
  status: Status | null | undefined,
): Status | null {
  if (status == null) {
    return null;
  }
  return statusIsFinalized({ status }) ? status : null;
}

type RunOutputSignals = Pick<
  WorkflowRunStatusApiResponseWithWorkflow,
  | "outputs"
  | "errors"
  | "downloaded_files"
  | "downloaded_file_urls"
  | "task_v2"
  | "webhook_failure_reason"
>;

// The pane-header indicator and RunView's Outputs tab both key off this; keep
// them routed through here so they can't drift. The extracted_information cast
// below is unsound on purpose — a string value must stay truthy via
// Object.values, matching what RunOutputsSection actually renders.
export function runHasOutputs(
  workflowRun: RunOutputSignals | null | undefined,
): boolean {
  if (!workflowRun) {
    return false;
  }
  const hasErrors =
    Array.isArray(workflowRun.errors) && workflowRun.errors.some(isRecord);
  const outputs = workflowRun.outputs;
  const extractedInformation =
    isRecord(outputs) && "extracted_information" in outputs
      ? (outputs.extracted_information as Record<string, unknown>)
      : null;
  const hasExtracted =
    extractedInformation != null &&
    Object.values(extractedInformation).some((value) => value !== null);
  // A raw-count check, not RunView's deduped file list — dedup only ever
  // shrinks a non-empty input, never zeroes it out, so truthiness matches.
  const hasDownloads =
    (workflowRun.downloaded_files?.length ?? 0) > 0 ||
    (workflowRun.downloaded_file_urls?.length ?? 0) > 0;
  const hasObserverOutput = workflowRun.task_v2?.output != null;
  const hasWebhookFailure =
    workflowRun.task_v2?.webhook_failure_reason != null ||
    workflowRun.webhook_failure_reason != null;
  return (
    hasErrors ||
    hasExtracted ||
    hasDownloads ||
    hasObserverOutput ||
    hasWebhookFailure
  );
}

export type FilmstripFrame = {
  id: string;
  index: number;
  blockId: string;
  blockLabel: string | null;
  isBlockStart: boolean;
  actionType: ActionType;
  label: string;
  status: Status;
  blockType: string | null;
  screenshotArtifactId: string | null;
  stepId: string | null;
  actionOrder: number | null;
};

export function actionLabel(action: ActionsApiResponse): string {
  const candidate =
    action.intention?.trim() ||
    action.description?.trim() ||
    action.reasoning?.trim();
  if (candidate) {
    // Goto actions surface as "page.goto <url>"; show just the URL.
    return candidate.replace(/^page\.goto\s+/i, "");
  }
  return ReadableActionTypes[action.action_type] ?? action.action_type;
}

/**
 * Flattens the run timeline tree into the ordered action frames the filmstrip
 * renders. Thought items (no screenshot) go to the details panel, not the strip.
 */
export function buildFilmstrip(
  timeline: WorkflowRunTimelineItem[] | undefined,
): FilmstripFrame[] {
  const frames: FilmstripFrame[] = [];

  // Raw timeline is newest-first; flatten chronologically so the strip reads
  // oldest→newest left-to-right and live-edge scroll lands on the newest action.
  const ordered = flattenTimelineChronologically(timeline ?? []);

  const walk = (items: WorkflowRunTimelineItem[]): void => {
    for (const item of items) {
      if (isBlockItem(item)) {
        const block = item.block;
        // block.actions is newest-first; reverse to oldest-first so the strip
        // matches the run timeline tree (which reverses it the same way).
        [...(block.actions ?? [])].reverse().forEach((action, i) => {
          frames.push({
            id: action.action_id ?? `${block.workflow_run_block_id}:${i}`,
            index: 0,
            blockId: block.workflow_run_block_id,
            blockLabel: block.label,
            isBlockStart: false,
            actionType: action.action_type,
            label: actionLabel(action),
            status: action.status,
            blockType: block.block_type ?? null,
            screenshotArtifactId: action.screenshot_artifact_id ?? null,
            stepId: action.step_id,
            actionOrder: action.action_order,
          });
        });
      }
      if (item.children.length > 0) {
        walk(item.children);
      }
    }
  };

  walk(ordered);

  frames.forEach((frame, i) => {
    const prev = i > 0 ? frames[i - 1] : undefined;
    frame.index = i + 1;
    frame.isBlockStart = !prev || prev.blockId !== frame.blockId;
  });

  return frames;
}

export type BlockRunState = {
  status: Status | null;
  actionCount: number;
  failureReason: string | null;
  duration: number | null;
};

/**
 * Per-block run state keyed by block label, driving the editor BlockCard's inline
 * status bar. For looped blocks the latest occurrence wins.
 */
export function buildBlockStatusMap(
  timeline: WorkflowRunTimelineItem[] | undefined,
): Record<string, BlockRunState> {
  const byLabel: Record<string, BlockRunState> = {};

  const walk = (items: WorkflowRunTimelineItem[]): void => {
    for (const item of items) {
      if (isBlockItem(item)) {
        const block = item.block;
        if (block.label) {
          byLabel[block.label] = {
            status: block.status,
            actionCount: block.actions?.length ?? 0,
            failureReason: block.failure_reason,
            duration: block.duration,
          };
        }
      }
      if (item.children.length > 0) {
        walk(item.children);
      }
    }
  };

  walk(timeline ?? []);
  return byLabel;
}

/** action_id → action, for the run details panel to look up a pinned frame. */
export function buildActionIndex(
  timeline: WorkflowRunTimelineItem[] | undefined,
): Map<string, ActionsApiResponse> {
  const index = new Map<string, ActionsApiResponse>();
  const walk = (items: WorkflowRunTimelineItem[]): void => {
    for (const item of items) {
      if (isBlockItem(item)) {
        for (const action of item.block.actions ?? []) {
          if (action.action_id) {
            index.set(action.action_id, action);
          }
        }
      }
      if (item.children.length > 0) {
        walk(item.children);
      }
    }
  };
  walk(timeline ?? []);
  return index;
}
