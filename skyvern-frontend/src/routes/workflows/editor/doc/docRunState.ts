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

/** The plain-words headline for a run that failed before any step row exists. */
export function runFailureHeadline(run: {
  status: string;
  failure_reason?: string | null;
}): string | null {
  if (!FAILED.has(run.status) || !run.failure_reason?.trim()) return null;
  return failureHeadlineInPlainWords(
    run.failure_reason,
    (raw) => formatFailureReason(raw).headline,
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
  // A few plain words naming the item; null when it has no readable name.
  title: string | null;
  // The item's full value, for a disclosure under the line.
  detail: string | null;
  state: "running" | "succeeded" | "failed" | "waiting" | "canceled";
  failureHeadline: string | null;
};

export type DocLoopProgress = { total: number | null; items: DocLoopItem[] };

/**
 * The timeline as the ended run left it: a row the backend has not yet moved off Running ended with
 * the run, so it reads as the run's failure or cancel, not as still running. A terminated run is left
 * alone: its lagging rows read "Stopped", and its Stop step must keep its own status.
 */
export function settleRowsOfEndedRun(
  timeline: WorkflowRunTimelineItem[],
  run: { status: string; failure_reason?: string | null },
): WorkflowRunTimelineItem[] {
  const canceled = run.status === Status.Canceled;
  const failed = run.status === Status.Failed || run.status === Status.TimedOut;
  if (!canceled && !failed) return timeline;
  const settle = (block: WorkflowRunBlock): WorkflowRunBlock => {
    const lagging =
      block.status === Status.Running || block.status === Status.Paused;
    if (canceled)
      return lagging ? { ...block, status: Status.Canceled } : block;
    return lagging && block.block_type !== "terminate"
      ? {
          ...block,
          status: run.status as Status,
          failure_reason: block.failure_reason || run.failure_reason || null,
        }
      : block;
  };
  const walk = (items: WorkflowRunTimelineItem[]): WorkflowRunTimelineItem[] =>
    items.map((item) =>
      isBlockItem(item)
        ? {
            ...item,
            block: settle(item.block),
            task_v2_blocks: item.task_v2_blocks?.map(settle),
            children: walk(item.children),
          }
        : { ...item, children: walk(item.children) },
    );
  return walk(timeline);
}

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
  if (blocks.some((b) => b.status === Status.Canceled)) return "canceled";
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

const TITLE_KEYS = ["title", "name", "label", "heading", "subject"];
const SHORT = 60;

function shortText(value: unknown): string | null {
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  if (typeof value !== "string") return null;
  const text = value.trim().replace(/\s+/g, " ");
  if (!text) return null;
  return text.length > SHORT ? `${text.slice(0, SHORT - 1)}…` : text;
}

/** A loop item's name in a few words and its full value; raw objects never go on the line. */
export function loopItemText(
  value: unknown,
  currentValue: string | null,
): Pick<DocLoopItem, "title" | "detail"> {
  if (value !== undefined && value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    const key = Array.isArray(value)
      ? undefined
      : TITLE_KEYS.find((k) => shortText(record[k]));
    return {
      title: key ? shortText(record[key]) : null,
      detail: JSON.stringify(value, null, 2),
    };
  }
  const text = value ?? currentValue;
  if (text === undefined || text === null) return { title: null, detail: null };
  const raw = String(text);
  // The run row stores objects as their Python text; it is not a name.
  if (/^\s*[[{(]/.test(raw)) return { title: null, detail: raw };
  const title = shortText(raw);
  return { title, detail: title === raw.trim() ? null : raw };
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
              ...loopItemText(
                Array.isArray(block.loop_values)
                  ? block.loop_values[index]
                  : undefined,
                blocks.find((b) => b.current_value)?.current_value ?? null,
              ),
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

// Every saved version stamps its own ids and times on each step and input, so only the authored fields compare.
const STAMPED_KEYS = new Set([
  "output_parameter",
  "workflow_id",
  "created_at",
  "modified_at",
  "deleted_at",
]);
const isStamped = (key: string) =>
  STAMPED_KEYS.has(key) || key.endsWith("_parameter_id");
const isStep = (
  value: unknown,
): value is Record<string, unknown> & { label: string } =>
  Boolean(value) &&
  typeof value === "object" &&
  typeof (value as Record<string, unknown>).label === "string" &&
  typeof (value as Record<string, unknown>).block_type === "string";
// A conditional branch's "id" is minted again on every save; an "id" anywhere else is authored.
const isBranch = (holder: Record<string, unknown>) =>
  "is_default" in holder || "next_block_label" in holder;

// The editor's first save fills defaults an agent made elsewhere left empty (null or "" fields,
// the title as the label, the unused export schema), so those read as absent. A step inside
// another counts by its label only: editing it changes that step, not the one around it.
function authored(step: Record<string, unknown>): string {
  return JSON.stringify(
    step,
    function (this: Record<string, unknown>, key: string, value: unknown) {
      if (key === "") return value;
      if (value === null) return undefined;
      // Only a step's or an input's own keys; the same names inside an authored schema or body are edits.
      const own = isStep(this) || typeof this.parameter_type === "string";
      if (own && (isStamped(key) || value === "")) return undefined;
      if (key === "id" && isBranch(this)) return undefined;
      if (isStep(this)) {
        if (key === "title" && value === this.label) return undefined;
        if (key === "export_data_schema" && !this.export_enabled)
          return undefined;
      }
      return isStep(value) ? `step:${value.label}` : value;
    },
  );
}

function stepsByLabel(node: unknown, into: Map<string, string>) {
  if (Array.isArray(node)) {
    for (const child of node) stepsByLabel(child, into);
    return;
  }
  if (!node || typeof node !== "object") return;
  const record = node as Record<string, unknown>;
  if (isStep(record)) into.set(record.label, authored(record));
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

/** The labels of the steps a run's saved version had. */
export function stepLabelsOf(definition: unknown): Set<string> {
  const labels = new Map<string, string>();
  stepsByLabel(definition, labels);
  return new Set(labels.keys());
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

type SavedVersion = {
  version: number;
  workflow_definition: unknown;
  deleted_at?: string | null;
};

/** Whether a run's results belong to a version other than the agent's saved one. */
export function ranAnotherVersion(
  ran: SavedVersion,
  current: SavedVersion,
  newer: { copilotTest: boolean; proposalPending: boolean },
): boolean {
  // Accepting a tested proposal saves a newer version with the same steps; its test run still speaks for it.
  // A newer Copilot test with no proposal left under review was rejected (its draft version is deleted once
  // the run ends). Any other newer version is a save this tab has not loaded yet, so its run is the agent's.
  const discarded =
    ran.version < current.version ||
    (ran.version > current.version &&
      !newer.proposalPending &&
      (newer.copilotTest || Boolean(ran.deleted_at)));
  return (
    discarded &&
    !sameSteps(ran.workflow_definition, current.workflow_definition)
  );
}

export type DocBlockRuns = {
  count: number;
  failed: number;
  canceled: number;
  withOutput: number;
};

/** How many times each step ran in the run (once per loop item), how many failed, and how many returned a value. */
export function collectBlockRuns(
  timeline: WorkflowRunTimelineItem[],
  workflowRunId: string,
  into: Map<string, DocBlockRuns>,
  hasOutput: (block: WorkflowRunBlock) => boolean,
) {
  for (const block of descendants(timeline)) {
    if (!block.label || block.workflow_run_id !== workflowRunId) continue;
    // A row that never started (skipped branch, not reached) is not a run.
    if (
      block.status === Status.Skipped ||
      block.status === Status.Queued ||
      block.status === Status.Created
    )
      continue;
    const runs = into.get(block.label) ?? {
      count: 0,
      failed: 0,
      canceled: 0,
      withOutput: 0,
    };
    runs.count += 1;
    if (FAILED.has(block.status)) runs.failed += 1;
    if (block.status === Status.Canceled) runs.canceled += 1;
    if (hasOutput(block)) runs.withOutput += 1;
    into.set(block.label, runs);
  }
}
