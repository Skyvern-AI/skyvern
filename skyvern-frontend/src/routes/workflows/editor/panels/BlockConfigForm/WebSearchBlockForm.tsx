import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { AppNode, isWorkflowBlockNode } from "../../nodes";
import { WebSearchEditor } from "../../nodes/WebSearchNode/WebSearchEditor";
import { WebSearchNode } from "../../nodes/WebSearchNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function WebSearchBlockForm({ blockId }: { blockId: string }) {
  const reactFlowInstance = useReactFlow<AppNode>();
  const node = reactFlowInstance.getNode(blockId);

  if (!node || !isWorkflowBlockNode(node) || node.type !== "web_search") {
    return null;
  }

  return (
    <WebSearchBlockFormBody blockId={blockId} node={node as WebSearchNode} />
  );
}

function WebSearchBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: WebSearchNode;
}) {
  const debounceValue = useMemo(
    () => ({
      query: node.data.query,
      provider: node.data.provider,
      numResults: node.data.numResults,
      prompt: node.data.prompt,
      jsonSchema: node.data.jsonSchema,
      model: node.data.model,
      parameterKeys: node.data.parameterKeys,
    }),
    [
      node.data.query,
      node.data.provider,
      node.data.numResults,
      node.data.prompt,
      node.data.jsonSchema,
      node.data.model,
      node.data.parameterKeys,
    ],
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

  return <WebSearchEditor blockId={blockId} />;
}

export { WebSearchBlockForm };
