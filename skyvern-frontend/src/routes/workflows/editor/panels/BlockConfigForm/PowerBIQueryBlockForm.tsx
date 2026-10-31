import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { type PowerBIQueryNode } from "../../nodes/PowerBIQueryNode/types";
import { PowerBIQueryEditor } from "../../nodes/PowerBIQueryNode/PowerBIQueryEditor";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function PowerBIQueryBlockForm({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "powerBIQuery") {
    return null;
  }
  return <PowerBIQueryBlockFormBody blockId={blockId} node={node} />;
}

function PowerBIQueryBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: PowerBIQueryNode;
}) {
  const { connectionId, datasetId, query, maxRows, parameterKeys } = node.data;
  const value = useMemo(
    () => ({ connectionId, datasetId, query, maxRows, parameterKeys }),
    [connectionId, datasetId, query, maxRows, parameterKeys],
  );
  const { commit } = useDebouncedSidebarSave({ blockId, value });

  useEffect(() => {
    const store = usePendingCommitsStore.getState();
    store.register(blockId, commit);
    return () => store.unregister(blockId);
  }, [blockId, commit]);

  return <PowerBIQueryEditor blockId={blockId} />;
}

export { PowerBIQueryBlockForm };
