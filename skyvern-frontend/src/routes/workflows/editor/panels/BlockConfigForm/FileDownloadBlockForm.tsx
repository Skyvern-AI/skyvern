import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { FileDownloadEditor } from "../../nodes/FileDownloadNode/FileDownloadEditor";
import {
  isFileDownloadNode,
  type FileDownloadNode,
} from "../../nodes/FileDownloadNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function FileDownloadBlockForm({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || !isFileDownloadNode(node)) {
    return null;
  }
  return (
    <FileDownloadBlockFormBody
      blockId={blockId}
      node={node as FileDownloadNode}
    />
  );
}

function FileDownloadBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: FileDownloadNode;
}) {
  const data = node.data;

  const value = useMemo(
    () => ({
      url: data.url,
      navigationGoal: data.navigationGoal,
      downloadTimeout: data.downloadTimeout,
      model: data.model,
      parameterKeys: data.parameterKeys,
      engine: data.engine,
      maxStepsOverride: data.maxStepsOverride,
      errorCodeMapping: data.errorCodeMapping,
      continueOnFailure: data.continueOnFailure,
      nextLoopOnFailure: data.nextLoopOnFailure,
      disableCache: data.disableCache,
      downloadSuffix: data.downloadSuffix,
      totpIdentifier: data.totpIdentifier,
      totpVerificationUrl: data.totpVerificationUrl,
    }),
    [
      data.url,
      data.navigationGoal,
      data.downloadTimeout,
      data.model,
      data.parameterKeys,
      data.engine,
      data.maxStepsOverride,
      data.errorCodeMapping,
      data.continueOnFailure,
      data.nextLoopOnFailure,
      data.disableCache,
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
    return () => store.unregister(blockId);
  }, [blockId, commit]);

  return <FileDownloadEditor blockId={blockId} />;
}

export { FileDownloadBlockForm };
