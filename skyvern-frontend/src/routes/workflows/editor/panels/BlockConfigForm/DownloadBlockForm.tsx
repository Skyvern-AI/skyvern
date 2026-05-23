import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { DownloadEditor } from "../../nodes/DownloadNode/DownloadEditor";
import type { DownloadNode } from "../../nodes/DownloadNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function DownloadBlockForm({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "download") {
    return null;
  }
  return (
    <DownloadBlockFormBody blockId={blockId} node={node as DownloadNode} />
  );
}

function DownloadBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: DownloadNode;
}) {
  const { url } = node.data;

  const value = useMemo(() => ({ url }), [url]);
  const { commit } = useDebouncedSidebarSave({
    blockId,
    value,
  });

  useEffect(() => {
    const store = usePendingCommitsStore.getState();
    store.register(blockId, commit);
    return () => store.unregister(blockId);
  }, [blockId, commit]);

  return <DownloadEditor blockId={blockId} />;
}

export { DownloadBlockForm };
