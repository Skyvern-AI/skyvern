import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { isWorkflowBlockNode, type AppNode } from "../../nodes";
import { PdfFillEditor } from "../../nodes/PdfFillNode/PdfFillEditor";
import { isPdfFillNode, type PdfFillNode } from "../../nodes/PdfFillNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function PdfFillBlockForm({ blockId }: { blockId: string }) {
  const { getNode } = useReactFlow<AppNode>();
  const node = getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || !isPdfFillNode(node)) {
    return null;
  }
  return <PdfFillBlockFormBody blockId={blockId} node={node as PdfFillNode} />;
}

function PdfFillBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: PdfFillNode;
}) {
  const {
    fileUrl,
    prompt,
    payload,
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
      payload,
      llmKey,
      parameterKeys,
      model,
      ignoreWorkflowSystemPrompt,
      continueOnFailure,
    }),
    [
      fileUrl,
      prompt,
      payload,
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

  return <PdfFillEditor blockId={blockId} />;
}

export { PdfFillBlockForm };
