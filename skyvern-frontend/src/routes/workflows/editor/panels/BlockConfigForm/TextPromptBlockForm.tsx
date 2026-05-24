import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { TextPromptEditor } from "../../nodes/TextPromptNode/TextPromptEditor";
import {
  isTextPromptNode,
  type TextPromptNode,
} from "../../nodes/TextPromptNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function TextPromptBlockForm({ blockId }: { blockId: string }) {
  const reactFlow = useReactFlow<AppNode>();
  const node = reactFlow.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || !isTextPromptNode(node)) {
    return null;
  }
  return <TextPromptBlockFormBody blockId={blockId} node={node} />;
}

function TextPromptBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: TextPromptNode;
}) {
  const { prompt, jsonSchema, model } = node.data;

  const debounceValue = useMemo(
    () => ({ prompt, jsonSchema, model }),
    [prompt, jsonSchema, model],
  );
  const { commit } = useDebouncedSidebarSave({
    blockId,
    value: debounceValue,
  });

  useEffect(() => {
    const store = usePendingCommitsStore.getState();
    store.register(blockId, commit);
    return () => {
      store.unregister(blockId);
    };
  }, [blockId, commit]);

  return <TextPromptEditor blockId={blockId} />;
}

export { TextPromptBlockForm };
