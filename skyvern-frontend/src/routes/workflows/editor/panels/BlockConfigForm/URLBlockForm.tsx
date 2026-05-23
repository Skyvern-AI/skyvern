import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { URLEditor } from "../../nodes/URLNode/URLEditor";
import { isUrlNode } from "../../nodes/URLNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function URLBlockForm({ blockId }: { blockId: string }) {
  const reactFlowInstance = useReactFlow<AppNode>();
  const node = reactFlowInstance.getNode(blockId);

  const isUrl =
    node !== undefined && isWorkflowBlockNode(node) && isUrlNode(node);
  const url = isUrl ? node.data.url : "";

  const value = useMemo(() => ({ url }), [url]);
  const { commit } = useDebouncedSidebarSave({
    blockId,
    value,
  });

  useEffect(() => {
    const store = usePendingCommitsStore.getState();
    store.register(blockId, commit);
    return () => {
      store.unregister(blockId);
    };
  }, [blockId, commit]);

  if (!isUrl) {
    return null;
  }

  return <URLEditor blockId={blockId} />;
}

export { URLBlockForm };
