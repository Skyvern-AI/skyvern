import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { PDFParserEditor } from "../../nodes/PDFParserNode/PDFParserEditor";
import type { PDFParserNode } from "../../nodes/PDFParserNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function PDFParserBlockForm({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "pdfParser") {
    return null;
  }
  return (
    <PDFParserBlockFormBody blockId={blockId} node={node as PDFParserNode} />
  );
}

function PDFParserBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: PDFParserNode;
}) {
  const { fileUrl, jsonSchema, model } = node.data;

  const value = useMemo(
    () => ({ fileUrl, jsonSchema, model }),
    [fileUrl, jsonSchema, model],
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

  return <PDFParserEditor blockId={blockId} />;
}

export { PDFParserBlockForm };
