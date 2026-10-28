import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { AgentStepEditor } from "../../nodes/AgentStepNode/AgentStepEditor";
import type { AgentStepNode } from "../../nodes/AgentStepNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function AgentStepBlockForm({ blockId }: { blockId: string }) {
  const reactFlow = useReactFlow<AppNode>();
  const node = reactFlow.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "agentStep") {
    return null;
  }
  return <AgentStepBlockFormBody blockId={blockId} node={node} />;
}

function AgentStepBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: AgentStepNode;
}) {
  const value = useMemo(
    () => ({
      prompt: node.data.prompt,
      dataSchema: node.data.dataSchema,
      maxTurns: node.data.maxTurns,
    }),
    [node.data.prompt, node.data.dataSchema, node.data.maxTurns],
  );
  const { commit } = useDebouncedSidebarSave({ blockId, value });

  useEffect(() => {
    usePendingCommitsStore.getState().register(blockId, commit);
    return () => usePendingCommitsStore.getState().unregister(blockId);
  }, [blockId, commit]);

  return <AgentStepEditor blockId={blockId} />;
}

export { AgentStepBlockForm };
