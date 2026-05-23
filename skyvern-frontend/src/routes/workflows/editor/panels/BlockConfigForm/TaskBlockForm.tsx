import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { TaskEditor } from "../../nodes/TaskNode/TaskEditor";
import type { TaskNode } from "../../nodes/TaskNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function TaskBlockForm({ blockId }: { blockId: string }) {
  const reactFlowInstance = useReactFlow<AppNode>();
  const node = reactFlowInstance.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "task") {
    return null;
  }
  return <TaskBlockFormBody blockId={blockId} node={node as TaskNode} />;
}

function TaskBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: TaskNode;
}) {
  const data = node.data;

  const value = useMemo(
    () => ({
      url: data.url,
      navigationGoal: data.navigationGoal,
      dataExtractionGoal: data.dataExtractionGoal,
      dataSchema: data.dataSchema,
      parameterKeys: data.parameterKeys,
      completeCriterion: data.completeCriterion,
      model: data.model,
      engine: data.engine,
      maxStepsOverride: data.maxStepsOverride,
      errorCodeMapping: data.errorCodeMapping,
      continueOnFailure: data.continueOnFailure,
      nextLoopOnFailure: data.nextLoopOnFailure,
      includeActionHistoryInVerification:
        data.includeActionHistoryInVerification,
      disableCache: data.disableCache,
      allowDownloads: data.allowDownloads,
      downloadSuffix: data.downloadSuffix,
      totpIdentifier: data.totpIdentifier,
      totpVerificationUrl: data.totpVerificationUrl,
    }),
    [
      data.url,
      data.navigationGoal,
      data.dataExtractionGoal,
      data.dataSchema,
      data.parameterKeys,
      data.completeCriterion,
      data.model,
      data.engine,
      data.maxStepsOverride,
      data.errorCodeMapping,
      data.continueOnFailure,
      data.nextLoopOnFailure,
      data.includeActionHistoryInVerification,
      data.disableCache,
      data.allowDownloads,
      data.downloadSuffix,
      data.totpIdentifier,
      data.totpVerificationUrl,
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

  return <TaskEditor blockId={blockId} />;
}

export { TaskBlockForm };
