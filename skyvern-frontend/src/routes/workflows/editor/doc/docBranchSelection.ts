import { Status } from "@/api/types";
import { isWorkflowBlockNode, type AppNode } from "../nodes";
import type { WorkflowRunBlock } from "../../types/workflowRunTypes";
import type { DocBranch, DocItem } from "./buildDocTree";

export function selectedStepIds(items: DocItem[]): string[] {
  return items.flatMap((item) =>
    item.kind === "note"
      ? []
      : item.kind === "unplaced"
        ? item.nodeIds
        : [
            item.nodeId,
            ...(item.kind === "loop"
              ? selectedStepIds(item.children)
              : item.kind === "conditional"
                ? item.branches.flatMap((b) => selectedStepIds(b.children))
                : []),
          ],
  );
}

export function docBranchSelection(
  item: Extract<DocItem, { kind: "conditional" }>,
  label: string,
  selectedId: string | undefined,
  byId: Map<string, AppNode>,
  blocksByLabel: Map<string, WorkflowRunBlock>,
) {
  const runBlock = blocksByLabel.get(label);
  // Saved branch IDs can change; executed child labels are the historical fallback.
  const stepRan = (branch: DocBranch) =>
    selectedStepIds(branch.children).some((id) => {
      const step = byId.get(id);
      return Boolean(
        step && isWorkflowBlockNode(step) && blocksByLabel.has(step.data.label),
      );
    });
  // A save mints new branch IDs on the server, so until a reload the run's ID may match none;
  // the condition it recorded (none for Otherwise) still names the branch.
  const byRecordedCondition = (block: WorkflowRunBlock) => {
    if (!block.executed_branch_id) return undefined;
    const expression = block.executed_branch_expression?.trim();
    return expression
      ? item.branches.find(
          (b) => b.kind !== "otherwise" && b.condition === expression,
        )
      : // The backend records Otherwise as null (never false); a field the run did not record at all is undefined.
        block.executed_branch_result === false ||
          block.executed_branch_result === null
        ? item.branches.find((b) => b.kind === "otherwise" && !b.synthetic)
        : undefined;
  };
  // A one-step run of a branch step has no row for the conditional; the step's own row names its branch.
  const ranBranch = !runBlock
    ? item.branches.find(stepRan)
    : runBlock.status === Status.Completed
      ? (item.branches.find(
          (b) => b.branchId === runBlock.executed_branch_id,
        ) ??
        byRecordedCondition(runBlock) ??
        item.branches.find(stepRan))
      : undefined;
  const selected =
    item.branches.find((b) => b.branchId === selectedId) ??
    ranBranch ??
    item.branches[0]!;
  // Inside a loop the conditional's row is the last item's; steps of another branch may have run on an earlier item.
  return { selected, ranBranch, selectedRan: stepRan(selected) };
}
