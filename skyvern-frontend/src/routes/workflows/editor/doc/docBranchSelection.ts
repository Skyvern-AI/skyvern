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
  const ranBranch =
    runBlock?.status === Status.Completed
      ? (item.branches.find(
          (b) => b.branchId === runBlock.executed_branch_id,
        ) ?? item.branches.find(stepRan))
      : undefined;
  const selected =
    item.branches.find((b) => b.branchId === selectedId) ??
    ranBranch ??
    item.branches[0]!;
  return { selected, ranBranch };
}
