import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode } from "../../nodes";
import { WorkflowSettingsEditor } from "../../nodes/StartNode/WorkflowSettingsEditor";
import { isStartNode } from "../../nodes/StartNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function WorkflowSettingsBlockForm({ blockId }: { blockId: string }) {
  const { getNode } = useReactFlow<AppNode>();
  const node = getNode(blockId);
  if (!node || !isStartNode(node) || !node.data?.withWorkflowSettings) {
    return null;
  }
  return <WorkflowSettingsBlockFormBody blockId={blockId} data={node.data} />;
}

function WorkflowSettingsBlockFormBody({
  blockId,
  data,
}: {
  blockId: string;
  data: Extract<AppNode, { type: "start" }>["data"];
}) {
  const value = useMemo(() => {
    if (!data.withWorkflowSettings) {
      return {};
    }
    return {
      model: data.model,
      webhookCallbackUrl: data.webhookCallbackUrl,
      proxyLocation: data.proxyLocation,
      runWith: data.runWith,
      aiFallback: data.aiFallback,
      scriptCacheKey: data.scriptCacheKey,
      runSequentially: data.runSequentially,
      sequentialKey: data.sequentialKey,
      persistBrowserSession: data.persistBrowserSession,
      extraHttpHeaders: data.extraHttpHeaders,
      maxScreenshotScrolls: data.maxScreenshotScrolls,
      finallyBlockLabel: data.finallyBlockLabel,
      workflowSystemPrompt: data.workflowSystemPrompt,
    };
  }, [data]);

  const { commit } = useDebouncedSidebarSave({ blockId, value });

  useEffect(() => {
    const store = usePendingCommitsStore.getState();
    store.register(blockId, commit);
    return () => {
      store.unregister(blockId);
    };
  }, [blockId, commit]);

  return <WorkflowSettingsEditor blockId={blockId} />;
}

export { WorkflowSettingsBlockForm };
