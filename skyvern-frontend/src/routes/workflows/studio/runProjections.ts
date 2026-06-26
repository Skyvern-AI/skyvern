import {
  ActionsApiResponse,
  ActionType,
  ReadableActionTypes,
  Status,
} from "@/api/types";
import { statusIsAFailureType } from "@/routes/tasks/types";
import {
  isBlockItem,
  WorkflowRunTimelineItem,
} from "@/routes/workflows/types/workflowRunTypes";
import { flattenTimelineChronologically } from "@/routes/workflows/workflowRun/workflowTimelineUtils";
import { normalizeUtcTimestamp } from "@/util/timeFormat";

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
  stepId: string | null;
  actionOrder: number | null;
};

function actionLabel(action: ActionsApiResponse): string {
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
