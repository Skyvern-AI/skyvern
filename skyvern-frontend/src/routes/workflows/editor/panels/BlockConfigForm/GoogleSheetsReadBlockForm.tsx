import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { GoogleSheetsReadEditor } from "../../nodes/GoogleSheetsReadNode/GoogleSheetsReadEditor";
import { type GoogleSheetsReadNode } from "../../nodes/GoogleSheetsReadNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function GoogleSheetsReadBlockForm({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "googleSheetsRead") {
    return null;
  }
  return (
    <GoogleSheetsReadBlockFormBody
      blockId={blockId}
      node={node as GoogleSheetsReadNode}
    />
  );
}

function GoogleSheetsReadBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: GoogleSheetsReadNode;
}) {
  const data = node.data;

  const value = useMemo(
    () => ({
      credentialId: data.credentialId,
      spreadsheetUrl: data.spreadsheetUrl,
      sheetName: data.sheetName,
      range: data.range,
      hasHeaderRow: data.hasHeaderRow,
      parameterKeys: data.parameterKeys,
    }),
    [
      data.credentialId,
      data.spreadsheetUrl,
      data.sheetName,
      data.range,
      data.hasHeaderRow,
      data.parameterKeys,
    ],
  );
  const { commit } = useDebouncedSidebarSave<typeof value>({
    blockId,
    value,
  });

  useEffect(() => {
    const store = usePendingCommitsStore.getState();
    store.register(blockId, commit);
    return () => store.unregister(blockId);
  }, [blockId, commit]);

  return <GoogleSheetsReadEditor blockId={blockId} />;
}

export { GoogleSheetsReadBlockForm };
