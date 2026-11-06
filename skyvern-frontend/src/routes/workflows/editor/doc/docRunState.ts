import { Status } from "@/api/types";
import {
  isBlockItem,
  type WorkflowRunBlock,
  type WorkflowRunTimelineItem,
} from "../../types/workflowRunTypes";
import { describeCodeBlockFailure } from "../../workflowRun/codeBlockFailure";
import { formatFailureReason } from "../../workflowRun/failureReasonFormat";
import { failureHeadlineInPlainWords } from "./docFailureText";

export type DocTagState =
  | "running"
  | "succeeded"
  | "failed"
  | "waiting"
  | "notReached";

type RunLike = { status: string } | null | undefined;

const FAILED = new Set<string | null>([
  Status.Failed,
  Status.TimedOut,
  Status.Terminated,
]);

/** The short headline the Run panel states for a failed block; one source for every Doc line. */
export function blockFailureHeadline(block: WorkflowRunBlock): string | null {
  if (!block.failure_reason?.trim()) return null;
  return (
    describeCodeBlockFailure(block)?.title ??
    failureHeadlineInPlainWords(
      block.failure_reason,
      (raw) => formatFailureReason(raw).headline,
    )
  );
}

// A Stop step that did its job ends "terminated"; one whose reason failed to render ends "failed".
export const stoppedOnPurpose = (block: WorkflowRunBlock) =>
  block.block_type === "terminate" &&
  (block.status === Status.Terminated || block.status === Status.Completed);

/** The state a step's tag shows for the Doc's run; null while the step has not started in a live run. */
export function docTagState(
  block: WorkflowRunBlock | undefined,
  runLive: boolean,
  hasRun: boolean,
): DocTagState | null {
  if (!hasRun) return null;
  if (!block) return runLive ? null : "notReached";
  // A Stop step ends the run on purpose: it did what the doc says.
  if (stoppedOnPurpose(block)) return "succeeded";
  switch (block.status) {
    case Status.Running:
      return runLive ? "running" : "notReached";
    case Status.Completed:
      return "succeeded";
    case Status.Paused:
      return "waiting";
    case Status.Queued:
    case Status.Created:
      return runLive ? null : "notReached";
    default:
      return FAILED.has(block.status) ? "failed" : "notReached";
  }
}

/** Whether the run ended at a Stop step, so the steps it never reached say so. */
export function docRunStoppedByStep(
  blocks: Map<string, WorkflowRunBlock>,
): boolean {
  for (const block of blocks.values()) if (stoppedOnPurpose(block)) return true;
  return false;
}

export type DocLoopItem = {
  index: number;
  value: string | null;
  state: "running" | "succeeded" | "failed" | "waiting";
  failureHeadline: string | null;
};

export type DocLoopProgress = { total: number | null; items: DocLoopItem[] };

function itemState(blocks: WorkflowRunBlock[]): DocLoopItem["state"] {
  if (blocks.some((b) => FAILED.has(b.status))) return "failed";
  if (blocks.some((b) => b.status === Status.Paused)) return "waiting";
  if (
    blocks.some(
      (b) =>
        b.status === Status.Running ||
        b.status === Status.Queued ||
        b.status === Status.Created,
    )
  )
    return "running";
  return "succeeded";
}

function descendants(items: WorkflowRunTimelineItem[]): WorkflowRunBlock[] {
  const out: WorkflowRunBlock[] = [];
  const walk = (list: WorkflowRunTimelineItem[]) => {
    for (const item of list) {
      if (isBlockItem(item)) out.push(item.block);
      walk(item.children);
    }
  };
  walk(items);
  return out;
}

/** Per-item progress of each loop in the run, keyed by the loop's label. */
export function collectLoopProgress(
  timeline: WorkflowRunTimelineItem[],
  workflowRunId: string,
  into: Map<string, DocLoopProgress>,
) {
  const walk = (list: WorkflowRunTimelineItem[]) => {
    for (const item of list) {
      if (!isBlockItem(item)) continue;
      const block = item.block;
      const isLoop =
        block.block_type === "for_loop" || block.block_type === "while_loop";
      if (
        isLoop &&
        block.label &&
        block.workflow_run_id === workflowRunId &&
        !into.has(block.label)
      ) {
        const groups = new Map<number, WorkflowRunTimelineItem[]>();
        for (const child of item.children) {
          if (!isBlockItem(child) || child.block.current_index === null)
            continue;
          const group = groups.get(child.block.current_index) ?? [];
          group.push(child);
          groups.set(child.block.current_index, group);
        }
        const items = [...groups.entries()]
          .sort(([a], [b]) => a - b)
          .map(([index, group]) => {
            const blocks = descendants(group);
            const failed = blocks.find((b) => FAILED.has(b.status));
            return {
              index,
              value: blocks.find((b) => b.current_value)?.current_value ?? null,
              state: itemState(blocks),
              failureHeadline: failed ? blockFailureHeadline(failed) : null,
            };
          });
        into.set(block.label, {
          total: Array.isArray(block.loop_values)
            ? block.loop_values.length
            : null,
          items,
        });
      }
      walk(item.children);
    }
  };
  walk(timeline);
}

/** One line under a loop: where it is while running, how it went when done. */
export function docLoopProgressText(
  progress: DocLoopProgress,
  live: boolean,
): string | null {
  const { total, items } = progress;
  if (!items.length) return null;
  const failed = items.filter((i) => i.state === "failed").length;
  const tail = failed ? ` · ${failed} failed` : "";
  if (live) {
    const at = items[items.length - 1]!.index + 1;
    return `Item ${at}${total ? ` of ${total}` : ""}${tail}`;
  }
  return `${items.length} item${items.length === 1 ? "" : "s"}${tail}`;
}

/** Whether every step row has finished, so the rows describe how the run ended. */
export function docRunRowsSettled(
  blocks: Map<string, WorkflowRunBlock>,
): boolean {
  // No rows yet means the timeline hasn't loaded, not that nothing ran.
  if (blocks.size === 0) return false;
  for (const block of blocks.values())
    if (
      block.status === Status.Running ||
      block.status === Status.Queued ||
      block.status === Status.Created
    )
      return false;
  return true;
}

/** The one toast at the end of a Doc run. */
export function docRunEndMessage(
  run: RunLike,
  blocks: Map<string, WorkflowRunBlock>,
  loops: Map<string, DocLoopProgress>,
): string | null {
  if (!run) return null;
  if (run.status === Status.Canceled) return "Run stopped";
  if (docRunStoppedByStep(blocks))
    return "Run stopped where the doc says to stop";
  if (FAILED.has(run.status)) return "Run failed. See the red step.";
  if (run.status !== Status.Completed) return null;
  let failedItems = 0;
  for (const loop of loops.values())
    failedItems += loop.items.filter((i) => i.state === "failed").length;
  return failedItems
    ? `Run finished · ${failedItems} item${failedItems === 1 ? "" : "s"} failed and ${failedItems === 1 ? "was" : "were"} skipped`
    : "Run finished";
}

// Every saved version stamps its own ids and times on each step, so only the authored fields compare.
const STAMPED_KEYS = new Set([
  "output_parameter",
  "created_at",
  "modified_at",
  "deleted_at",
]);
// A save writes defaults as null where the author left a field out, so null reads as absent.
const stamped = (key: string, value: unknown) =>
  STAMPED_KEYS.has(key) || value === null ? undefined : value;

function stepsByLabel(node: unknown, into: Map<string, string>) {
  if (Array.isArray(node)) {
    for (const child of node) stepsByLabel(child, into);
    return;
  }
  if (!node || typeof node !== "object") return;
  const record = node as Record<string, unknown>;
  if (typeof record.label === "string" && typeof record.block_type === "string")
    into.set(record.label, JSON.stringify(record, stamped));
  for (const value of Object.values(record)) stepsByLabel(value, into);
}

/**
 * Steps of the current agent that a run of an earlier version cannot speak for: added or edited
 * since. Their results are hidden, so a stale error never paints a step that did not have it.
 */
export function stepsUnknownToRun(
  runDefinition: unknown,
  currentDefinition: unknown,
): Set<string> {
  const ran = new Map<string, string>();
  const current = new Map<string, string>();
  stepsByLabel(runDefinition, ran);
  stepsByLabel(currentDefinition, current);
  const unknown = new Set<string>();
  for (const [label, definition] of current)
    if (ran.get(label) !== definition) unknown.add(label);
  return unknown;
}

function inputsOf(definition: unknown): string {
  const parameters = (definition as { parameters?: unknown } | null)
    ?.parameters;
  if (!Array.isArray(parameters)) return "[]";
  return JSON.stringify(
    parameters
      .map((p: Record<string, unknown>) => [
        p.key,
        p.parameter_type,
        p.workflow_parameter_type,
        p.default_value ?? null,
      ])
      .sort((a, b) => String(a[0]).localeCompare(String(b[0]))),
  );
}

/** Whether the run's version and the current one have the same steps and inputs, however they were saved. */
export function sameSteps(
  runDefinition: unknown,
  currentDefinition: unknown,
): boolean {
  const ran = new Map<string, string>();
  const current = new Map<string, string>();
  stepsByLabel(runDefinition, ran);
  stepsByLabel(currentDefinition, current);
  return (
    inputsOf(runDefinition) === inputsOf(currentDefinition) &&
    [...ran.keys()].join("\0") === [...current.keys()].join("\0") &&
    [...current].every(([label, definition]) => ran.get(label) === definition)
  );
}
