import { useNodesData } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { Label } from "@/components/ui/label";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import {
  getConditionLabel,
  orderBranchesWithDefaultsLast,
} from "../../nodes/ConditionalNode/branchDisplayUtils";
import {
  type ConditionalNodeData,
  defaultBranchCriteria,
} from "../../nodes/ConditionalNode/types";
import { useUpdate } from "../../useUpdate";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function ConditionalBlockForm({ blockId }: { blockId: string }) {
  const nodeSlice = useNodesData<AppNode>(blockId);
  if (
    !nodeSlice ||
    !isWorkflowBlockNode(nodeSlice as AppNode) ||
    nodeSlice.type !== "conditional"
  ) {
    return null;
  }
  return <ConditionalBlockFormBody blockId={blockId} data={nodeSlice.data} />;
}

function ConditionalBlockFormBody({
  blockId,
  data,
}: {
  blockId: string;
  data: ConditionalNodeData;
}) {
  const {
    branches,
    activeBranchId,
    mergeLabel,
    continueOnFailure,
    nextLoopOnFailure,
  } = data;
  const orderedBranches = useMemo(
    () => orderBranchesWithDefaultsLast(branches),
    [branches],
  );
  const update = useUpdate<ConditionalNodeData>({
    id: blockId,
    editable: data.editable,
  });

  const value = useMemo(
    () => ({
      branches,
      activeBranchId,
      mergeLabel,
      continueOnFailure,
      nextLoopOnFailure,
    }),
    [
      branches,
      activeBranchId,
      mergeLabel,
      continueOnFailure,
      nextLoopOnFailure,
    ],
  );
  const { commit } = useDebouncedSidebarSave<typeof value>({
    blockId,
    value,
  });

  useEffect(() => {
    const store = usePendingCommitsStore.getState();
    store.register(blockId, commit);
    return () => store.unregister(blockId);
  }, [blockId, commit]);

  const handleExpressionChange = (
    branchId: string,
    expression: string,
  ): void => {
    const targetBranch = branches.find((branch) => branch.id === branchId);
    if (!targetBranch || targetBranch.is_default) {
      return;
    }

    update({
      branches: branches.map((branch) => {
        if (branch.id !== branchId) {
          return branch;
        }
        return {
          ...branch,
          criteria: {
            ...(branch.criteria ?? { ...defaultBranchCriteria }),
            expression,
          },
        };
      }),
    });
  };

  return (
    <div data-testid="conditional-block-form" className="space-y-3">
      {orderedBranches.map((branch, index) => {
        const isDefaultBranch = branch.is_default;
        const branchExpression = isDefaultBranch
          ? "Executed when no other condition matches"
          : (branch.criteria?.expression ?? "");
        return (
          <div
            key={branch.id}
            className="space-y-2 rounded-md border border-border bg-slate-elevation2 p-3"
          >
            <div className="flex items-center justify-between gap-2">
              <Label className="text-xs text-tertiary-foreground">
                {getConditionLabel(branch, index)}
              </Label>
              {branch.id === activeBranchId && (
                <span className="rounded bg-slate-elevation5 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-tertiary-foreground">
                  Active
                </span>
              )}
            </div>
            <WorkflowBlockInputTextarea
              nodeId={blockId}
              value={branchExpression}
              disabled={!data.editable || isDefaultBranch}
              onChange={(nextValue) =>
                handleExpressionChange(branch.id, nextValue)
              }
              placeholder="Enter condition to evaluate (Jinja, natural language, or both)"
              className="nopan text-xs"
            />
          </div>
        );
      })}
    </div>
  );
}

export { ConditionalBlockForm };
