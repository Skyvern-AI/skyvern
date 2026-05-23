import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { ValidationEditor } from "../../nodes/ValidationNode/ValidationEditor";
import {
  isValidationNode,
  type ValidationNode,
} from "../../nodes/ValidationNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function ValidationBlockForm({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || !isValidationNode(node)) {
    return null;
  }
  return (
    <ValidationBlockFormBody blockId={blockId} node={node as ValidationNode} />
  );
}

function ValidationBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: ValidationNode;
}) {
  const data = node.data;

  const value = useMemo(
    () => ({
      completeCriterion: data.completeCriterion,
      terminateCriterion: data.terminateCriterion,
      errorCodeMapping: data.errorCodeMapping,
      parameterKeys: data.parameterKeys,
      model: data.model,
      continueOnFailure: data.continueOnFailure,
      nextLoopOnFailure: data.nextLoopOnFailure,
      disableCache: data.disableCache,
    }),
    [
      data.completeCriterion,
      data.terminateCriterion,
      data.errorCodeMapping,
      data.parameterKeys,
      data.model,
      data.continueOnFailure,
      data.nextLoopOnFailure,
      data.disableCache,
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

  return <ValidationEditor blockId={blockId} />;
}

export { ValidationBlockForm };
