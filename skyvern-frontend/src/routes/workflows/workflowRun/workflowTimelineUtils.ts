import { Status } from "@/api/types";
import { statusIsFinalized } from "@/routes/tasks/types";
import {
  isBlockItem,
  isThoughtItem,
  ObserverThought,
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import { WorkflowRunOverviewActiveElement } from "./WorkflowRunOverview";

const containerBlockTypes = new Set(["for_loop", "while_loop", "conditional"]);

function parseActiveIterationParam(value: string | null): number | null {
  if (value === null || !/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

function findBlockSurroundingAction(
  timeline: Array<WorkflowRunTimelineItem>,
  actionId: string,
): WorkflowRunBlock | undefined {
  const stack = [...timeline];
  while (stack.length > 0) {
    const current = stack.pop()!;
    if (current.type === "block") {
      const action = current.block.actions?.find(
        (action) => action.action_id === actionId,
      );
      if (action) {
        return current.block;
      }
    }
    if (current.children) {
      stack.push(...current.children);
    }
  }
}

function findBlockSurroundingThought(
  timeline: Array<WorkflowRunTimelineItem>,
  thoughtId: string,
): WorkflowRunBlock | undefined {
  let thought: ObserverThought | null = null;
  const stack = timeline.map((item) => ({
    item,
    parentBlock: undefined as WorkflowRunBlock | undefined,
  }));

  while (stack.length > 0) {
    const { item, parentBlock } = stack.pop()!;
    if (isThoughtItem(item) && item.thought.thought_id === thoughtId) {
      if (parentBlock) return parentBlock;
      thought = item.thought;
      break;
    }
    const nextParent = isBlockItem(item) ? item.block : parentBlock;
    for (const child of item.children) {
      stack.push({ item: child, parentBlock: nextParent });
    }
  }

  if (!thought) return undefined;

  const thoughtTime = new Date(thought.created_at).getTime();
  if (Number.isNaN(thoughtTime)) return undefined;

  let best: {
    block: WorkflowRunBlock;
    depth: number;
    duration: number;
  } | null = null;
  const blockStack = [...timeline].reverse().map((item) => ({
    item,
    depth: 0,
  }));
  while (blockStack.length > 0) {
    const { item, depth } = blockStack.pop()!;
    if (isBlockItem(item)) {
      const start = new Date(item.block.created_at).getTime();
      const end = new Date(item.block.modified_at).getTime();
      const isFinalized =
        item.block.status !== null &&
        statusIsFinalized({ status: item.block.status });
      const upperBound = isFinalized && !Number.isNaN(end) ? end : Infinity;
      if (
        !Number.isNaN(start) &&
        thoughtTime >= start &&
        thoughtTime <= upperBound
      ) {
        const duration = Number.isFinite(upperBound)
          ? upperBound - start
          : Number.POSITIVE_INFINITY;
        if (
          best === null ||
          depth > best.depth ||
          (depth === best.depth && duration < best.duration)
        ) {
          best = { block: item.block, depth, duration };
        }
      }
    }
    for (const child of [...item.children].reverse()) {
      blockStack.push({ item: child, depth: depth + 1 });
    }
  }

  return best?.block;
}

function findActiveItem(
  timeline: Array<WorkflowRunTimelineItem>,
  target: string | null,
  workflowRunIsFinalized: boolean,
  finallyBlockLabel?: string | null,
): WorkflowRunOverviewActiveElement {
  if (target === null) {
    if (!workflowRunIsFinalized) {
      return "stream";
    }
    // If there's a finally block, try to show it first when workflow is finalized
    if (finallyBlockLabel && timeline?.length > 0) {
      const finallyBlock = timeline.find(
        (item) => isBlockItem(item) && item.block.label === finallyBlockLabel,
      );
      if (finallyBlock && isBlockItem(finallyBlock)) {
        if (
          finallyBlock.block.actions &&
          finallyBlock.block.actions.length > 0
        ) {
          return finallyBlock.block.actions[0]!;
        }
        return finallyBlock.block;
      }
    }
    // Prefer the deepest last-executed leaf — the actual final unit of work,
    // not the outermost container that wraps it.
    const lastLeaf = findLastExecutedBlock(timeline);
    if (lastLeaf) {
      if (lastLeaf.actions && lastLeaf.actions.length > 0) {
        return lastLeaf.actions[0]!;
      }
      return lastLeaf;
    }
    if (timeline?.length > 0) {
      const timelineItem = timeline![0];
      if (isBlockItem(timelineItem)) {
        if (
          timelineItem.block.actions &&
          timelineItem.block.actions.length > 0
        ) {
          return timelineItem.block.actions[0]!;
        }
        return timelineItem.block;
      }
      if (isThoughtItem(timelineItem)) {
        return timelineItem.thought;
      }
    }
  }
  if (target === "stream") {
    return "stream";
  }
  const stack = [...timeline];
  while (stack.length > 0) {
    const current = stack.pop()!;
    if (
      current.type === "block" &&
      current.block.workflow_run_block_id === target
    ) {
      return current.block;
    }
    if (current.type === "thought" && current.thought.thought_id === target) {
      return current.thought;
    }
    if (current.type === "block") {
      const actions = current.block.actions;
      if (actions) {
        const activeAction = actions.find(
          (action) => action.action_id === target,
        );
        if (activeAction) {
          return activeAction;
        }
      }
    }
    if (current.children) {
      stack.push(...current.children);
    }
  }
  return null;
}

/**
 * Container blocks have no screenshots; fall back to the most recent
 * descendant leaf. Timeline children are DESC, so the first leaf wins.
 * When a specific loop iteration is selected, scope the walk to that
 * iteration's children so the screenshot tracks Panel B's iteration.
 */
function resolveScreenshotBlockId(
  timeline: Array<WorkflowRunTimelineItem>,
  block: WorkflowRunBlock,
  iterationIndex: number | null = null,
): string {
  if (!containerBlockTypes.has(block.block_type)) {
    return block.workflow_run_block_id;
  }

  const timelineItem = findTimelineBlockItem(
    timeline,
    block.workflow_run_block_id,
  );
  if (!timelineItem) {
    return block.workflow_run_block_id;
  }

  if (
    iterationIndex !== null &&
    (block.block_type === "for_loop" || block.block_type === "while_loop")
  ) {
    const iterationChildren = timelineItem.children.filter(
      (item) =>
        isBlockItem(item) && item.block.current_index === iterationIndex,
    );
    const descendantInIteration = findFirstLeafBlockId(iterationChildren);
    if (descendantInIteration) return descendantInIteration;
    // Iteration's children aren't present (stale URL / not yet executed);
    // fall through to the generic newest-leaf path.
  }

  const descendant = findFirstLeafBlockId(timelineItem.children);
  return descendant ?? block.workflow_run_block_id;
}

function findTimelineBlockItem(
  items: Array<WorkflowRunTimelineItem>,
  blockId: string,
): WorkflowRunTimelineItem | null {
  const stack = [...items];
  while (stack.length > 0) {
    const current = stack.pop()!;
    if (
      isBlockItem(current) &&
      current.block.workflow_run_block_id === blockId
    ) {
      return current;
    }
    if (current.children) {
      stack.push(...current.children);
    }
  }
  return null;
}

function findFirstLeafBlockId(
  items: Array<WorkflowRunTimelineItem>,
): string | null {
  const stack = [...items].reverse();
  while (stack.length > 0) {
    const item = stack.pop()!;
    if (isBlockItem(item)) {
      if (item.children.length > 0) {
        stack.push(...[...item.children].reverse());
        continue;
      }
      return item.block.workflow_run_block_id;
    }
  }
  return null;
}

/**
 * Deepest running block. An outer container can still be Running while
 * a descendant is doing the actual work — show the descendant.
 */
function findRunningBlock(
  timeline: Array<WorkflowRunTimelineItem>,
): WorkflowRunBlock | null {
  let best: WorkflowRunBlock | null = null;
  let bestDepth = -1;
  const stack = [...timeline].reverse().map((item) => ({ item, depth: 0 }));

  while (stack.length > 0) {
    const { item, depth } = stack.pop()!;
    if (isBlockItem(item) && item.block.status === Status.Running) {
      if (depth > bestDepth) {
        best = item.block;
        bestDepth = depth;
      }
    }
    for (const child of [...item.children].reverse()) {
      stack.push({ item: child, depth: depth + 1 });
    }
  }

  return best;
}

/**
 * Most-recent leaf in a terminal state. Filter to leaves: containers
 * always close last, so modified_at alone would pick the outer block.
 */
function findLastExecutedBlock(
  timeline: Array<WorkflowRunTimelineItem>,
): WorkflowRunBlock | null {
  let latest: WorkflowRunBlock | null = null;
  const stack = [...timeline];

  while (stack.length > 0) {
    const item = stack.pop()!;
    if (isBlockItem(item)) {
      const isLeaf = item.children.length === 0;
      if (
        isLeaf &&
        item.block.status !== null &&
        (statusIsFinalized({ status: item.block.status }) ||
          item.block.status === Status.Skipped)
      ) {
        if (
          latest === null ||
          new Date(item.block.modified_at).getTime() >
            new Date(latest.modified_at).getTime()
        ) {
          latest = item.block;
        }
      }
    }
    stack.push(...item.children);
  }

  return latest;
}

/**
 * Thoughts belong to a block by subtree OR by `created_at` within the
 * block's lifespan. Returned chronologically.
 */
function findThoughtsForBlock(
  timeline: Array<WorkflowRunTimelineItem>,
  block: WorkflowRunBlock,
): Array<ObserverThought> {
  const start = new Date(block.created_at).getTime();
  const end = new Date(block.modified_at).getTime();
  const thoughts: Array<ObserverThought> = [];
  const seen = new Set<string>();

  function collectInBlockSubtree(items: Array<WorkflowRunTimelineItem>) {
    const stack = [...items];
    while (stack.length > 0) {
      const item = stack.pop()!;
      if (isThoughtItem(item)) {
        if (!seen.has(item.thought.thought_id)) {
          seen.add(item.thought.thought_id);
          thoughts.push(item.thought);
        }
      }
      stack.push(...item.children);
    }
  }

  const stack = [...timeline];
  while (stack.length > 0) {
    const item = stack.pop()!;
    if (
      isBlockItem(item) &&
      item.block.workflow_run_block_id === block.workflow_run_block_id
    ) {
      collectInBlockSubtree(item.children);
    }
    if (isThoughtItem(item)) {
      const t = new Date(item.thought.created_at).getTime();
      if (t >= start && t <= end && !seen.has(item.thought.thought_id)) {
        seen.add(item.thought.thought_id);
        thoughts.push(item.thought);
      }
    }
    stack.push(...item.children);
  }

  thoughts.sort(
    (a, b) =>
      new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
  );
  return thoughts;
}

/**
 * Aggregate the status of an iteration's children. Returns Completed only
 * when every child is in a truly terminal state (Completed or Skipped).
 * Pending children (Created/Queued/Paused/null) surface as null so the
 * iteration row renders a neutral dot, not a misleading green check.
 */
function aggregateIterationStatus(
  items: Array<WorkflowRunTimelineItem>,
): Status | null {
  let hasRunning = false;
  let hasFailure = false;
  let hasNonTerminal = false;
  let hasAny = false;
  const stack = [...items];
  while (stack.length > 0) {
    const item = stack.pop()!;
    if (isBlockItem(item)) {
      hasAny = true;
      const s = item.block.status;
      if (s === Status.Running) {
        hasRunning = true;
      } else if (
        s === Status.Failed ||
        s === Status.Terminated ||
        s === Status.TimedOut ||
        s === Status.Canceled
      ) {
        hasFailure = true;
      } else if (
        s === null ||
        s === Status.Created ||
        s === Status.Queued ||
        s === Status.Paused
      ) {
        hasNonTerminal = true;
      }
    }
    stack.push(...item.children);
  }
  if (!hasAny) return null;
  if (hasFailure) return Status.Failed;
  if (hasRunning) return Status.Running;
  if (hasNonTerminal) return null;
  return Status.Completed;
}

export {
  aggregateIterationStatus,
  findActiveItem,
  findBlockSurroundingAction,
  findBlockSurroundingThought,
  findLastExecutedBlock,
  findRunningBlock,
  findThoughtsForBlock,
  parseActiveIterationParam,
  resolveScreenshotBlockId,
};
