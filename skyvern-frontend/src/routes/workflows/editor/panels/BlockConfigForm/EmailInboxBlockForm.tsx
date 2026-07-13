import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { EmailInboxEditor } from "../../nodes/EmailInboxNode/EmailInboxEditor";
import { type EmailInboxNode } from "../../nodes/EmailInboxNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function EmailInboxBlockForm({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "emailInbox") {
    return null;
  }
  return (
    <EmailInboxBlockFormBody blockId={blockId} node={node as EmailInboxNode} />
  );
}

function EmailInboxBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: EmailInboxNode;
}) {
  const data = node.data;

  const value = useMemo(
    () => ({
      emailClient: data.emailClient,
      credentialId: data.credentialId,
      folder: data.folder,
      prompt: data.prompt,
      sender: data.sender,
      subject: data.subject,
      newerThanDays: data.newerThanDays,
      maxResults: data.maxResults,
      includeBody: data.includeBody,
      parameterKeys: data.parameterKeys,
    }),
    [
      data.emailClient,
      data.credentialId,
      data.folder,
      data.prompt,
      data.sender,
      data.subject,
      data.newerThanDays,
      data.maxResults,
      data.includeBody,
      data.parameterKeys,
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

  return <EmailInboxEditor blockId={blockId} />;
}

export { EmailInboxBlockForm };
