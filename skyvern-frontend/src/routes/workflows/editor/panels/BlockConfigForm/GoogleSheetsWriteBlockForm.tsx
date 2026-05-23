import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { GoogleSheetsWriteEditor } from "../../nodes/GoogleSheetsWriteNode/GoogleSheetsWriteEditor";
import { type GoogleSheetsWriteNode } from "../../nodes/GoogleSheetsWriteNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function GoogleSheetsWriteBlockForm({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (
    !node ||
    !isWorkflowBlockNode(node) ||
    node.type !== "googleSheetsWrite"
  ) {
    return null;
  }
  return (
    <GoogleSheetsWriteBlockFormBody
      blockId={blockId}
      node={node as GoogleSheetsWriteNode}
    />
  );
}

function GoogleSheetsWriteBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: GoogleSheetsWriteNode;
}) {
  const data = node.data;

  const value = useMemo(
    () => ({
      credentialId: data.credentialId,
      spreadsheetUrl: data.spreadsheetUrl,
      sheetName: data.sheetName,
      range: data.range,
      writeMode: data.writeMode,
      values: data.values,
      columnMapping: data.columnMapping,
      createSheetIfMissing: data.createSheetIfMissing,
      parameterKeys: data.parameterKeys,
    }),
    [
      data.credentialId,
      data.spreadsheetUrl,
      data.sheetName,
      data.range,
      data.writeMode,
      data.values,
      data.columnMapping,
      data.createSheetIfMissing,
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

  return <GoogleSheetsWriteEditor blockId={blockId} />;
}

export { GoogleSheetsWriteBlockForm };
