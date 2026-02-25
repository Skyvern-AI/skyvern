import {
  isBlockItem,
  isThoughtItem,
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import { WorkflowRunOverviewActiveElement } from "./WorkflowRunOverview";

const containerBlockTypes = new Set(["for_loop", "conditional"]);

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
 * For container blocks (for_loop, conditional) that don't have their own
 * screenshots, find the first descendant leaf block whose artifacts can be shown.
 * Timeline children are ordered most-recent-first (DESC), so the first leaf
 * block we encounter is the most recent one.
 */
function resolveScreenshotBlockId(
  timeline: Array<WorkflowRunTimelineItem>,
  block: WorkflowRunBlock,
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
  for (const item of items) {
    if (isBlockItem(item)) {
      if (item.children.length > 0) {
        const childResult = findFirstLeafBlockId(item.children);
        if (childResult) return childResult;
      }
      return item.block.workflow_run_block_id;
    }
  }
  return null;
}

export { findActiveItem, findBlockSurroundingAction, resolveScreenshotBlockId };
