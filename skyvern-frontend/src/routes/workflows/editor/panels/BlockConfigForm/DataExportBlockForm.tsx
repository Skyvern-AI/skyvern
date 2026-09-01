import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { DataExportEditor } from "../../nodes/DataExportNode/DataExportEditor";
import type { DataExportNode } from "../../nodes/DataExportNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function DataExportBlockForm({ blockId }: { blockId: string }) {
  const reactFlow = useReactFlow<AppNode>();
  const node = reactFlow.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "dataExport") {
    return null;
  }
  return <DataExportBlockFormBody blockId={blockId} node={node} />;
}

function DataExportBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: DataExportNode;
}) {
  const value = useMemo(
    () => ({
      data: node.data.data,
      dataSchema: node.data.dataSchema,
      fileName: node.data.fileName,
      parameterKeys: node.data.parameterKeys,
    }),
    [
      node.data.data,
      node.data.dataSchema,
      node.data.fileName,
      node.data.parameterKeys,
    ],
  );
  const { commit } = useDebouncedSidebarSave({ blockId, value });

  useEffect(() => {
    usePendingCommitsStore.getState().register(blockId, commit);
    return () => usePendingCommitsStore.getState().unregister(blockId);
  }, [blockId, commit]);

  return <DataExportEditor blockId={blockId} />;
}

export { DataExportBlockForm };
