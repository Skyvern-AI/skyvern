import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { AppNode, isWorkflowBlockNode } from "../../nodes";
import { TerminateEditor } from "../../nodes/TerminateNode/TerminateEditor";
import { TerminateNode } from "../../nodes/TerminateNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function TerminateBlockForm({ blockId }: { blockId: string }) {
  const reactFlowInstance = useReactFlow<AppNode>();
  const node = reactFlowInstance.getNode(blockId);

  if (!node || !isWorkflowBlockNode(node) || node.type !== "terminate") {
    return null;
  }

  return (
    <TerminateBlockFormBody blockId={blockId} node={node as TerminateNode} />
  );
}

function TerminateBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: TerminateNode;
}) {
  const debounceValue = useMemo(
    () => ({ reason: node.data.reason }),
    [node.data.reason],
  );
  const { commit } = useDebouncedSidebarSave({
    blockId,
    value: debounceValue,
  });

  useEffect(() => {
    usePendingCommitsStore.getState().register(blockId, commit);
    return () => {
      usePendingCommitsStore.getState().unregister(blockId);
    };
  }, [blockId, commit]);

  return <TerminateEditor blockId={blockId} />;
}

export { TerminateBlockForm };
