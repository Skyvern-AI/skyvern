import { useNodesData } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { IntegrationActionEditor } from "../../nodes/IntegrationActionNode/IntegrationActionNode";
import type { IntegrationActionNode } from "../../nodes/IntegrationActionNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function IntegrationActionBlockForm({ blockId }: { blockId: string }) {
  const node = useNodesData<IntegrationActionNode>(blockId);
  const connectionId = node?.data.connectionId;
  const inputs = node?.data.inputs;
  const parameterKeys = node?.data.parameterKeys;
  const value = useMemo(
    () => ({ connectionId, inputs, parameterKeys }),
    [connectionId, inputs, parameterKeys],
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

  if (node?.type !== "integrationAction") return null;
  return <IntegrationActionEditor blockId={blockId} />;
}

export { IntegrationActionBlockForm };
