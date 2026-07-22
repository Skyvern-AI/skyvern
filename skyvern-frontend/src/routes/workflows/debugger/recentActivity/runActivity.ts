import { formatMs, toDate } from "@/util/utils";
import { formatDuration, toDuration } from "@/routes/workflows/utils";

import { workflowBlockTitle } from "../../editor/nodes/types";
import type {
  WorkflowBlock,
  WorkflowBlockType,
} from "../../types/workflowTypes";
import type { DebugSessionRun } from "../../hooks/useDebugSessionRunsQuery";

export type RunStatusKind =
  | "success"
  | "failure"
  | "running"
  | "pending"
  | "neutral";

const FAILURE_STATUSES = new Set([
  "failed",
  "terminated",
  "timed_out",
  "canceled",
]);

export function getRunStatusKind(
  status: string,
  isWorkflowRunning: boolean,
): RunStatusKind {
  if (status === "completed") return "success";
  if (FAILURE_STATUSES.has(status)) return "failure";
  // A run can be left in "running" after the workflow itself finalized (e.g. a
  // cancelled session); only spin while the workflow is genuinely live.
  if (status === "running") return isWorkflowRunning ? "running" : "neutral";
  if (status === "queued" || status === "created") return "pending";
  return "neutral";
}

export const STATUS_PILL_TONE: Record<RunStatusKind, string> = {
  success: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  failure: "bg-red-500/15 text-red-700 dark:text-red-300",
  running: "bg-sky-500/15 text-sky-700 dark:text-sky-300",
  pending: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  neutral: "bg-slate-500/15 text-tertiary-foreground",
};

export function getRunDurationLabel(run: DebugSessionRun): string | null {
  const begin = run.started_at
    ? toDate(run.started_at, null)
    : run.created_at
      ? toDate(run.created_at, null)
      : null;
  const end = run.finished_at ? toDate(run.finished_at, null) : null;
  if (!begin || !end) return null;
  const seconds = Math.round((end.getTime() - begin.getTime()) / 1000);
  if (seconds < 0) return null;
  return formatDuration(toDuration(seconds));
}

export function getRunAgoLabel(
  run: Pick<DebugSessionRun, "created_at">,
  now: number,
): string | null {
  const dt = run.created_at ? toDate(run.created_at, null) : null;
  if (!dt) return null;
  return formatMs(now - dt.getTime()).ago;
}

export function getRunAbsoluteTime(
  run: Pick<DebugSessionRun, "created_at">,
): string | null {
  const dt = run.created_at ? toDate(run.created_at, null) : null;
  return dt ? dt.toLocaleString() : null;
}

export function getRunModeLabel(run: DebugSessionRun): string {
  if (run.run_with === "code") return "Code";
  if (run.run_with === "agent") return "Agent";
  return "Unknown";
}

export function getRunStatusLabel(status: string): string {
  return status
    .replace(/_/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

export function getRunActivityKey(
  run: Pick<DebugSessionRun, "workflow_run_id" | "block_label">,
): string {
  return `${run.workflow_run_id}:${run.block_label}`;
}

export function normalizeReason(
  value: string | null | undefined,
): string | null {
  const normalized = value?.replace(/\s+/g, " ").trim();
  return normalized ? normalized : null;
}

export function getBlockTypeTitle(
  blockType: WorkflowBlockType | undefined,
): string {
  return blockType ? workflowBlockTitle[blockType] : "Block";
}

// `DebugSessionRun` only carries a `block_label`; the block's type (and thus its
// icon/title) lives in the workflow definition. Walk top-level blocks plus loop
// children so nested blocks resolve too; extend this if non-loop containers gain
// child block arrays.
export function buildBlockTypeByLabel(
  blocks: Array<WorkflowBlock> | undefined,
  accumulator: Map<string, WorkflowBlockType> = new Map(),
): Map<string, WorkflowBlockType> {
  if (!blocks) return accumulator;
  for (const block of blocks) {
    accumulator.set(block.label, block.block_type);
    if (block.block_type === "for_loop" || block.block_type === "while_loop") {
      buildBlockTypeByLabel(block.loop_blocks, accumulator);
    }
  }
  return accumulator;
}
