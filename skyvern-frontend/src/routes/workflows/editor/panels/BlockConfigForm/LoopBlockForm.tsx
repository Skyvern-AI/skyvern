import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { LoopEditor } from "../../nodes/LoopNode/LoopEditor";
import { type LoopNode } from "../../nodes/LoopNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function LoopBlockForm({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "loop") return null;
  return <LoopBlockFormBody blockId={blockId} node={node as LoopNode} />;
}

function LoopBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: LoopNode;
}) {
  const {
    loopKind,
    loopVariableReference,
    dataSchema,
    completeIfEmpty,
    continueOnFailure,
    nextLoopOnFailure,
    whileConditionExpression,
    whileConditionCriteriaType,
    whileConditionDescription,
  } = node.data;

  const value = useMemo(
    () => ({
      loopKind,
      loopVariableReference,
      dataSchema,
      completeIfEmpty,
      continueOnFailure,
      nextLoopOnFailure,
      whileConditionExpression,
      whileConditionCriteriaType,
      whileConditionDescription,
    }),
    [
      loopKind,
      loopVariableReference,
      dataSchema,
      completeIfEmpty,
      continueOnFailure,
      nextLoopOnFailure,
      whileConditionExpression,
      whileConditionCriteriaType,
      whileConditionDescription,
    ],
  );
  const { commit } = useDebouncedSidebarSave({
    blockId,
    value,
  });

  useEffect(() => {
    const store = usePendingCommitsStore.getState();
    store.register(blockId, commit);
    return () => store.unregister(blockId);
  }, [blockId, commit]);

  return <LoopEditor blockId={blockId} />;
}

export { LoopBlockForm };
