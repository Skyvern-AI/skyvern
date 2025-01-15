import {
  isBlockItem,
  isThoughtItem,
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import { WorkflowRunOverviewActiveElement } from "./WorkflowRunOverview";

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
): WorkflowRunOverviewActiveElement {
  if (target === null) {
    if (!workflowRunIsFinalized) {
      return "stream";
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

export { findActiveItem, findBlockSurroundingAction };
