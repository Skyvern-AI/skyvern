import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { UploadEditor } from "../../nodes/UploadNode/UploadEditor";
import type { UploadNode } from "../../nodes/UploadNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function UploadBlockForm({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "upload") {
    return null;
  }
  return <UploadBlockFormBody blockId={blockId} node={node as UploadNode} />;
}

function UploadBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: UploadNode;
}) {
  const { path } = node.data;

  const value = useMemo(() => ({ path }), [path]);
  const { commit } = useDebouncedSidebarSave({
    blockId,
    value,
  });

  useEffect(() => {
    const store = usePendingCommitsStore.getState();
    store.register(blockId, commit);
    return () => store.unregister(blockId);
  }, [blockId, commit]);

  return <UploadEditor blockId={blockId} />;
}

export { UploadBlockForm };
