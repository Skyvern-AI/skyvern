import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { AppNode, isWorkflowBlockNode } from "../../nodes";
import { BranchesEditor } from "../../nodes/ConditionalNode/BranchesEditor";
import {
  type ConditionalNode,
  type ConditionalNodeData,
} from "../../nodes/ConditionalNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function ConditionalBlockForm({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "conditional") {
    return null;
  }
  return (
    <ConditionalBlockFormBody
      blockId={blockId}
      node={node as ConditionalNode}
    />
  );
}

function ConditionalBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: ConditionalNode;
}) {
  const data = node.data;
  const {
    branches,
    activeBranchId,
    mergeLabel,
    continueOnFailure,
    nextLoopOnFailure,
  } = data;

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

  return (
    <div data-testid="conditional-block-form" className="space-y-4">
      <BranchesEditor nodeId={blockId} data={data as ConditionalNodeData} />
    </div>
  );
}

export { ConditionalBlockForm };
