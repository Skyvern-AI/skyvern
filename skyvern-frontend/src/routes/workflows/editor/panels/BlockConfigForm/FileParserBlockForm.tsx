import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { isWorkflowBlockNode, type AppNode } from "../../nodes";
import { FileParserEditor } from "../../nodes/FileParserNode/FileParserEditor";
import {
  isFileParserNode,
  type FileParserNode,
} from "../../nodes/FileParserNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function FileParserBlockForm({ blockId }: { blockId: string }) {
  const { getNode } = useReactFlow<AppNode>();
  const node = getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || !isFileParserNode(node)) {
    return null;
  }
  return (
    <FileParserBlockFormBody blockId={blockId} node={node as FileParserNode} />
  );
}

function FileParserBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: FileParserNode;
}) {
  const { fileUrl, fileType, jsonSchema, model } = node.data;

  const value = useMemo(
    () => ({ fileUrl, fileType, jsonSchema, model }),
    [fileUrl, fileType, jsonSchema, model],
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

  return <FileParserEditor blockId={blockId} />;
}

export { FileParserBlockForm };
