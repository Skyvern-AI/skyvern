import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode } from "../../nodes";
import { NavigationEditor } from "../../nodes/NavigationNode/NavigationEditor";
import {
  isNavigationNode,
  type NavigationNodeData,
} from "../../nodes/NavigationNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function NavigationBlockForm({ blockId }: { blockId: string }) {
  const reactFlowInstance = useReactFlow<AppNode>();
  const node = reactFlowInstance.getNode(blockId);
  if (!node || !isNavigationNode(node)) {
    return null;
  }
  return <NavigationBlockFormBody blockId={blockId} data={node.data} />;
}

function NavigationBlockFormBody({
  blockId,
  data,
}: {
  blockId: string;
  data: NavigationNodeData;
}) {
  const value = useMemo(
    () => ({
      url: data.url,
      navigationGoal: data.navigationGoal,
      prompt: data.prompt,
      engine: data.engine,
      model: data.model,
      maxSteps: data.maxSteps,
      maxStepsOverride: data.maxStepsOverride,
      parameterKeys: data.parameterKeys,
      completeCriterion: data.completeCriterion,
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
      data.prompt,
      data.engine,
      data.model,
      data.maxSteps,
      data.maxStepsOverride,
      data.parameterKeys,
      data.completeCriterion,
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
    const store = usePendingCommitsStore.getState();
    store.register(blockId, commit);
    return () => {
      store.unregister(blockId);
    };
  }, [blockId, commit]);

  return <NavigationEditor blockId={blockId} />;
}

export { NavigationBlockForm };
