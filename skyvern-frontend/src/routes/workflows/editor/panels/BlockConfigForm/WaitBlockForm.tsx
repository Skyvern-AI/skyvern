import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { AppNode, isWorkflowBlockNode } from "../../nodes";
import { WaitEditor } from "../../nodes/WaitNode/WaitEditor";
import { WaitNode } from "../../nodes/WaitNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function WaitBlockForm({ blockId }: { blockId: string }) {
  const reactFlowInstance = useReactFlow<AppNode>();
  const node = reactFlowInstance.getNode(blockId);

  if (!node || !isWorkflowBlockNode(node) || node.type !== "wait") {
    return null;
  }

  return <WaitBlockFormBody blockId={blockId} node={node as WaitNode} />;
}

function WaitBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: WaitNode;
}) {
  const debounceValue = useMemo(
    () => ({ waitInSeconds: node.data.waitInSeconds }),
    [node.data.waitInSeconds],
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

  return <WaitEditor blockId={blockId} />;
}

export { WaitBlockForm };
