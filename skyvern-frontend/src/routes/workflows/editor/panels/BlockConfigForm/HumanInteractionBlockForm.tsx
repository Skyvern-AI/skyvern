import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { HumanInteractionEditor } from "../../nodes/HumanInteractionNode/HumanInteractionEditor";
import { type HumanInteractionNode } from "../../nodes/HumanInteractionNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function HumanInteractionBlockForm({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (
    !node ||
    !isWorkflowBlockNode(node) ||
    node.type !== "human_interaction"
  ) {
    return null;
  }
  return (
    <HumanInteractionBlockFormBody
      blockId={blockId}
      node={node as HumanInteractionNode}
    />
  );
}

function HumanInteractionBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: HumanInteractionNode;
}) {
  const {
    instructions,
    timeoutSeconds,
    recipients,
    subject,
    body,
    negativeDescriptor,
    positiveDescriptor,
  } = node.data;

  const value = useMemo(
    () => ({
      instructions,
      timeoutSeconds,
      recipients,
      subject,
      body,
      negativeDescriptor,
      positiveDescriptor,
    }),
    [
      instructions,
      timeoutSeconds,
      recipients,
      subject,
      body,
      negativeDescriptor,
      positiveDescriptor,
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

  return <HumanInteractionEditor blockId={blockId} />;
}

export { HumanInteractionBlockForm };
