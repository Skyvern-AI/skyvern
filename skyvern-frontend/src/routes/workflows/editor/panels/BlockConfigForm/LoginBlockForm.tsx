import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode } from "../../nodes";
import { LoginEditor } from "../../nodes/LoginNode/LoginEditor";
import { isLoginNode, type LoginNode } from "../../nodes/LoginNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function LoginBlockForm({ blockId }: { blockId: string }) {
  const reactFlowInstance = useReactFlow<AppNode>();
  const node = reactFlowInstance.getNode(blockId);
  if (!node || !isLoginNode(node)) {
    return null;
  }
  return <LoginBlockFormBody blockId={blockId} node={node as LoginNode} />;
}

function LoginBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: LoginNode;
}) {
  const data = node.data;

  const value = useMemo(
    () => ({
      url: data.url,
      navigationGoal: data.navigationGoal,
      parameterKeys: data.parameterKeys,
      model: data.model,
      completeCriterion: data.completeCriterion,
      engine: data.engine,
      maxStepsOverride: data.maxStepsOverride,
      errorCodeMapping: data.errorCodeMapping,
      continueOnFailure: data.continueOnFailure,
      nextLoopOnFailure: data.nextLoopOnFailure,
      includeActionHistoryInVerification:
        data.includeActionHistoryInVerification,
      disableCache: data.disableCache,
      totpIdentifier: data.totpIdentifier,
      totpVerificationUrl: data.totpVerificationUrl,
    }),
    [
      data.url,
      data.navigationGoal,
      data.parameterKeys,
      data.model,
      data.completeCriterion,
      data.engine,
      data.maxStepsOverride,
      data.errorCodeMapping,
      data.continueOnFailure,
      data.nextLoopOnFailure,
      data.includeActionHistoryInVerification,
      data.disableCache,
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

  return <LoginEditor blockId={blockId} />;
}

export { LoginBlockForm };
