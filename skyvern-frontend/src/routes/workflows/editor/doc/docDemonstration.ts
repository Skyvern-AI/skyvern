import type { WorkflowCopilotDemonstrationInsertion } from "../../copilot/workflowCopilotTypes";
import type { DocItem } from "./buildDocTree";

/**
 * The index to draw a recording's status line before, among a top-level sequence: after the step the
 * recording follows (and its trailing notes), else before the step it precedes (and its notes above),
 * else at the end. -1 when the insertion sits inside a nested scope.
 */
export function demonstrationLineIndex(
  items: DocItem[],
  labelOf: (nodeId: string) => string | undefined,
  insertion: WorkflowCopilotDemonstrationInsertion | null,
): number {
  const stepAt = (label: string | null | undefined) =>
    label
      ? items.findIndex(
          (item) =>
            (item.kind === "step" ||
              item.kind === "loop" ||
              item.kind === "conditional") &&
            labelOf(item.nodeId) === label,
        )
      : -1;
  const after = stepAt(insertion?.after_label);
  if (after !== -1) {
    const owner = items[after] as { nodeId: string };
    let index = after + 1;
    for (;;) {
      const next = items[index];
      if (
        next?.kind === "note" &&
        next.placement === "trailing" &&
        next.ownerNodeId === owner.nodeId
      )
        index += 1;
      else return index;
    }
  }
  const before = stepAt(insertion?.before_label);
  if (before !== -1) {
    const owner = items[before] as { nodeId: string };
    let index = before;
    for (;;) {
      const previous = items[index - 1];
      if (
        previous?.kind === "note" &&
        previous.placement === "above" &&
        previous.ownerNodeId === owner.nodeId
      )
        index -= 1;
      else return index;
    }
  }
  // Both labels name steps inside a loop or branch, which this sequence does not hold.
  if (insertion?.after_label || insertion?.before_label) return -1;
  // No step to anchor on: the end of the sequence, which is before a step that always runs last.
  const last = items.findIndex(
    (item) =>
      (item.kind === "step" ||
        item.kind === "loop" ||
        item.kind === "conditional") &&
      item.finally,
  );
  if (last === -1) return items.length;
  let index = last;
  while (
    index > 0 &&
    items[index - 1]?.kind === "note" &&
    (items[index - 1] as { placement: string }).placement === "above" &&
    (items[index - 1] as { ownerNodeId: string }).ownerNodeId ===
      (items[last] as { nodeId: string }).nodeId
  )
    index -= 1;
  return index;
}
