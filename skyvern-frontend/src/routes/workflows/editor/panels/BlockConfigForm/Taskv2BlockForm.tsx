import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { Taskv2Editor } from "../../nodes/Taskv2Node/Taskv2Editor";
import { isTaskV2Node, type Taskv2Node } from "../../nodes/Taskv2Node/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function Taskv2BlockForm({ blockId }: { blockId: string }) {
  const { getNode } = useReactFlow<AppNode>();
  const node = getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || !isTaskV2Node(node)) {
    return null;
  }
  return <Taskv2BlockFormBody blockId={blockId} node={node as Taskv2Node} />;
}

function Taskv2BlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: Taskv2Node;
}) {
  const data = node.data;

  const value = useMemo(
    () => ({
      url: data.url,
      prompt: data.prompt,
      model: data.model,
      maxSteps: data.maxSteps,
      disableCache: data.disableCache,
      totpIdentifier: data.totpIdentifier,
      totpVerificationUrl: data.totpVerificationUrl,
    }),
    [
      data.url,
      data.prompt,
      data.model,
      data.maxSteps,
      data.disableCache,
      data.totpIdentifier,
      data.totpVerificationUrl,
    ],
  );
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

  return <Taskv2Editor blockId={blockId} />;
}

export { Taskv2BlockForm };
