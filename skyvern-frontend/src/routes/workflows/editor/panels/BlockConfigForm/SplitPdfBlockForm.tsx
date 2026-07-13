import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { isWorkflowBlockNode, type AppNode } from "../../nodes";
import { SplitPdfEditor } from "../../nodes/SplitPdfNode/SplitPdfEditor";
import {
  isSplitPdfNode,
  type SplitPdfNode,
} from "../../nodes/SplitPdfNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function SplitPdfBlockForm({ blockId }: { blockId: string }) {
  const { getNode } = useReactFlow<AppNode>();
  const node = getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || !isSplitPdfNode(node)) {
    return null;
  }
  return (
    <SplitPdfBlockFormBody blockId={blockId} node={node as SplitPdfNode} />
  );
}

function SplitPdfBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: SplitPdfNode;
}) {
  const {
    fileUrl,
    prompt,
    llmKey,
    parameterKeys,
    model,
    ignoreWorkflowSystemPrompt,
    continueOnFailure,
  } = node.data;

  const value = useMemo(
    () => ({
      fileUrl,
      prompt,
      llmKey,
      parameterKeys,
      model,
      ignoreWorkflowSystemPrompt,
      continueOnFailure,
    }),
    [
      fileUrl,
      prompt,
      llmKey,
      parameterKeys,
      model,
      ignoreWorkflowSystemPrompt,
      continueOnFailure,
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

  return <SplitPdfEditor blockId={blockId} />;
}

export { SplitPdfBlockForm };
