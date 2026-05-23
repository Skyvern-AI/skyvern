import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { PrintPageEditor } from "../../nodes/PrintPageNode/PrintPageEditor";
import type { PrintPageNode } from "../../nodes/PrintPageNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function PrintPageBlockForm({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "printPage") {
    return null;
  }
  return (
    <PrintPageBlockFormBody blockId={blockId} node={node as PrintPageNode} />
  );
}

function PrintPageBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: PrintPageNode;
}) {
  const {
    format,
    printBackground,
    includeTimestamp,
    customFilename,
    landscape,
    parameterKeys,
  } = node.data;

  const value = useMemo(
    () => ({
      format,
      printBackground,
      includeTimestamp,
      customFilename,
      landscape,
      parameterKeys,
    }),
    [
      format,
      printBackground,
      includeTimestamp,
      customFilename,
      landscape,
      parameterKeys,
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

  return <PrintPageEditor blockId={blockId} />;
}

export { PrintPageBlockForm };
