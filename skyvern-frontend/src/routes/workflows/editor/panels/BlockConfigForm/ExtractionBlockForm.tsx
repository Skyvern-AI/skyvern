import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { ExtractionEditor } from "../../nodes/ExtractionNode/ExtractionEditor";
import type { ExtractionNode } from "../../nodes/ExtractionNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function ExtractionBlockForm({ blockId }: { blockId: string }) {
  const reactFlowInstance = useReactFlow<AppNode>();
  const node = reactFlowInstance.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "extraction") {
    return null;
  }
  return (
    <ExtractionBlockFormBody blockId={blockId} node={node as ExtractionNode} />
  );
}

function ExtractionBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: ExtractionNode;
}) {
  const data = node.data;

  const value = useMemo(
    () => ({
      dataExtractionGoal: data.dataExtractionGoal,
      dataSchema: data.dataSchema,
      model: data.model,
      parameterKeys: data.parameterKeys,
      engine: data.engine,
      maxStepsOverride: data.maxStepsOverride,
      continueOnFailure: data.continueOnFailure,
      nextLoopOnFailure: data.nextLoopOnFailure,
      disableCache: data.disableCache,
    }),
    [
      data.dataExtractionGoal,
      data.dataSchema,
      data.model,
      data.parameterKeys,
      data.engine,
      data.maxStepsOverride,
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
    usePendingCommitsStore.getState().register(blockId, commit);
    return () => {
      usePendingCommitsStore.getState().unregister(blockId);
    };
  }, [blockId, commit]);

  return <ExtractionEditor blockId={blockId} />;
}

export { ExtractionBlockForm };
