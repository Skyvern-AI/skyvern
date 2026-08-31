import { statusIsAFailureType } from "@/routes/tasks/types";
import {
  isBlockItem,
  type WorkflowRunBlock,
  type WorkflowRunTimelineItem,
} from "@/routes/workflows/types/workflowRunTypes";
import { flattenTimelineChronologically } from "@/routes/workflows/workflowRun/workflowTimelineUtils";

/**
 * The block the run died on: the last failed block in walk order that
 * was allowed to end the run (continue_on_failure blocks can't). The walk takes
 * top-level items oldest-first and descends into each before moving on, so a
 * failed container yields to its failing leaf. That ordering is what makes the
 * last match the culprit — a run stops at the block that killed it, so there is
 * normally only one.
 *
 * Prefer the pre-finally failure: a finally block may run after it and fail on
 * its own. If there is no earlier failure, though, the failed finally block is
 * the only useful jump target.
 */
export function failingBlock(
  timeline: ReadonlyArray<WorkflowRunTimelineItem> | undefined,
  finallyBlockLabel: string | null = null,
): WorkflowRunBlock | null {
  if (!timeline) {
    return null;
  }
  let found: WorkflowRunBlock | null = null;
  let finallyFailure: WorkflowRunBlock | null = null;
  const walk = (items: ReadonlyArray<WorkflowRunTimelineItem>): void => {
    for (const item of items) {
      if (
        isBlockItem(item) &&
        item.block.status &&
        statusIsAFailureType({ status: item.block.status }) &&
        !item.block.continue_on_failure
      ) {
        if (finallyBlockLabel && item.block.label === finallyBlockLabel) {
          finallyFailure = item.block;
        } else {
          found = item.block;
        }
      }
      if (item.children.length > 0) {
        walk(item.children);
      }
    }
  };
  walk(flattenTimelineChronologically([...timeline]));
  return found ?? finallyFailure;
}
