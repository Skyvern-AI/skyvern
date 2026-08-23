import { statusIsAFailureType } from "@/routes/tasks/types";
import {
  isBlockItem,
  type WorkflowRunTimelineItem,
} from "@/routes/workflows/types/workflowRunTypes";
import { flattenTimelineChronologically } from "@/routes/workflows/workflowRun/workflowTimelineUtils";

/**
 * The labeled block the run died on: the last failed block in walk order that
 * was allowed to end the run (continue_on_failure blocks can't). The walk takes
 * top-level items oldest-first and descends into each before moving on, so a
 * failed container yields to its failing leaf. That ordering is what makes the
 * last match the culprit — a run stops at the block that killed it, so there is
 * normally only one.
 *
 * The finally block is excluded because it runs AFTER the failure and can fail
 * on its own; the run's failure_reason (which seeds the copilot message) is the
 * pre-finally one, so counting it would name a block the message never mentions.
 */
export function failingBlockLabel(
  timeline: ReadonlyArray<WorkflowRunTimelineItem> | undefined,
  finallyBlockLabel: string | null = null,
): string | null {
  if (!timeline) {
    return null;
  }
  let found: string | null = null;
  const walk = (items: ReadonlyArray<WorkflowRunTimelineItem>): void => {
    for (const item of items) {
      if (
        isBlockItem(item) &&
        item.block.label &&
        item.block.label !== finallyBlockLabel &&
        item.block.status &&
        statusIsAFailureType({ status: item.block.status }) &&
        !item.block.continue_on_failure
      ) {
        found = item.block.label;
      }
      if (item.children.length > 0) {
        walk(item.children);
      }
    }
  };
  walk(flattenTimelineChronologically([...timeline]));
  return found;
}
