import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { SendEmailEditor } from "../../nodes/SendEmailNode/SendEmailEditor";
import { type SendEmailNode } from "../../nodes/SendEmailNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function SendEmailBlockForm({ blockId }: { blockId: string }) {
  const reactFlowInstance = useReactFlow<AppNode>();
  const node = reactFlowInstance.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "sendEmail") {
    return null;
  }
  return (
    <SendEmailBlockFormBody blockId={blockId} node={node as SendEmailNode} />
  );
}

function SendEmailBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: SendEmailNode;
}) {
  const { recipients, subject, body, fileAttachments } = node.data;

  const value = useMemo(
    () => ({ recipients, subject, body, fileAttachments }),
    [recipients, subject, body, fileAttachments],
  );
  const { commit } = useDebouncedSidebarSave({
    blockId,
    value,
  });

  useEffect(() => {
    const store = usePendingCommitsStore.getState();
    store.register(blockId, commit);
    return () => {
      usePendingCommitsStore.getState().unregister(blockId);
    };
  }, [blockId, commit]);

  return <SendEmailEditor blockId={blockId} />;
}

export { SendEmailBlockForm };
