import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { HttpRequestEditor } from "../../nodes/HttpRequestNode/HttpRequestEditor";
import { type HttpRequestNode } from "../../nodes/HttpRequestNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function HttpRequestBlockForm({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "http_request") {
    return null;
  }
  return (
    <HttpRequestBlockFormBody
      blockId={blockId}
      node={node as HttpRequestNode}
    />
  );
}

function HttpRequestBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: HttpRequestNode;
}) {
  const data = node.data;

  const value = useMemo(
    () => ({
      method: data.method,
      url: data.url,
      headers: data.headers,
      body: data.body,
      files: data.files,
      timeout: data.timeout,
      followRedirects: data.followRedirects,
      parameterKeys: data.parameterKeys,
      downloadFilename: data.downloadFilename,
      saveResponseAsFile: data.saveResponseAsFile,
      continueOnFailure: data.continueOnFailure,
    }),
    [
      data.method,
      data.url,
      data.headers,
      data.body,
      data.files,
      data.timeout,
      data.followRedirects,
      data.parameterKeys,
      data.downloadFilename,
      data.saveResponseAsFile,
      data.continueOnFailure,
    ],
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

  return <HttpRequestEditor blockId={blockId} />;
}

export { HttpRequestBlockForm };
