import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { WorkflowTriggerEditor } from "../../nodes/WorkflowTriggerNode/WorkflowTriggerEditor";
import { type WorkflowTriggerNode } from "../../nodes/WorkflowTriggerNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function WorkflowTriggerBlockForm({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "workflowTrigger") {
    return null;
  }
  return (
    <WorkflowTriggerBlockFormBody
      blockId={blockId}
      node={node as WorkflowTriggerNode}
    />
  );
}

function WorkflowTriggerBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: WorkflowTriggerNode;
}) {
  const data = node.data;

  const value = useMemo(
    () => ({
      workflowPermanentId: data.workflowPermanentId,
      workflowTitle: data.workflowTitle,
      payload: data.payload,
      waitForCompletion: data.waitForCompletion,
      browserSessionId: data.browserSessionId,
      useParentBrowserSession: data.useParentBrowserSession,
      parameterKeys: data.parameterKeys,
      continueOnFailure: data.continueOnFailure,
      nextLoopOnFailure: data.nextLoopOnFailure,
    }),
    [
      data.workflowPermanentId,
      data.workflowTitle,
      data.payload,
      data.waitForCompletion,
      data.browserSessionId,
      data.useParentBrowserSession,
      data.parameterKeys,
      data.continueOnFailure,
      data.nextLoopOnFailure,
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

  return <WorkflowTriggerEditor blockId={blockId} />;
}

export { WorkflowTriggerBlockForm };
