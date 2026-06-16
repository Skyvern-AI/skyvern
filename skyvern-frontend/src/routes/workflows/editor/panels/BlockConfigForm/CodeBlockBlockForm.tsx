import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { CodeBlockEditor } from "../../nodes/CodeBlockNode/CodeBlockEditor";
import type { CodeBlockNode } from "../../nodes/CodeBlockNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function CodeBlockBlockForm({ blockId }: { blockId: string }) {
  const reactFlowInstance = useReactFlow<AppNode>();
  const node = reactFlowInstance.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "codeBlock") {
    return null;
  }
  return (
    <CodeBlockBlockFormBody blockId={blockId} node={node as CodeBlockNode} />
  );
}

function CodeBlockBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: CodeBlockNode;
}) {
  const { code, parameterKeys, prompt } = node.data;

  const value = useMemo(
    () => ({
      code,
      parameterKeys,
      prompt,
    }),
    [code, parameterKeys, prompt],
  );
  const { commit } = useDebouncedSidebarSave({
    blockId,
    value,
  });

  useEffect(() => {
    usePendingCommitsStore.getState().register(blockId, commit);
    return () => {
      usePendingCommitsStore.getState().unregister(blockId);
    };
  }, [blockId, commit]);

  return <CodeBlockEditor blockId={blockId} />;
}

export { CodeBlockBlockForm };
