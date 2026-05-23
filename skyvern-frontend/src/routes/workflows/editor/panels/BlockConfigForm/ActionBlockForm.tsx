import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { ActionEditor } from "../../nodes/ActionNode/ActionEditor";
import { type ActionNode } from "../../nodes/ActionNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function ActionBlockForm({ blockId }: { blockId: string }) {
  const reactFlowInstance = useReactFlow<AppNode>();
  const node = reactFlowInstance.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "action") {
    return null;
  }
  return <ActionBlockFormBody blockId={blockId} node={node as ActionNode} />;
}

function ActionBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: ActionNode;
}) {
  const data = node.data;

  const value = useMemo(
    () => ({
      url: data.url,
      navigationGoal: data.navigationGoal,
      errorCodeMapping: data.errorCodeMapping,
      allowDownloads: data.allowDownloads,
      downloadSuffix: data.downloadSuffix,
      parameterKeys: data.parameterKeys,
      totpVerificationUrl: data.totpVerificationUrl,
      totpIdentifier: data.totpIdentifier,
      disableCache: data.disableCache,
      engine: data.engine,
      model: data.model,
      continueOnFailure: data.continueOnFailure,
      nextLoopOnFailure: data.nextLoopOnFailure,
    }),
    [
      data.url,
      data.navigationGoal,
      data.errorCodeMapping,
      data.allowDownloads,
      data.downloadSuffix,
      data.parameterKeys,
      data.totpVerificationUrl,
      data.totpIdentifier,
      data.disableCache,
      data.engine,
      data.model,
      data.continueOnFailure,
      data.nextLoopOnFailure,
    ],
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

  return <ActionEditor blockId={blockId} />;
}

export { ActionBlockForm };
