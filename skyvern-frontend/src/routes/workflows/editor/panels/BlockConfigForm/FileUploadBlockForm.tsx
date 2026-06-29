import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { type AppNode, isWorkflowBlockNode } from "../../nodes";
import { FileUploadEditor } from "../../nodes/FileUploadNode/FileUploadEditor";
import { type FileUploadNode } from "../../nodes/FileUploadNode/types";
import { useDebouncedSidebarSave } from "../useDebouncedSidebarSave";

function FileUploadBlockForm({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "fileUpload") {
    return null;
  }
  return (
    <FileUploadBlockFormBody blockId={blockId} node={node as FileUploadNode} />
  );
}

function FileUploadBlockFormBody({
  blockId,
  node,
}: {
  blockId: string;
  node: FileUploadNode;
}) {
  const {
    storageType,
    path,
    s3Bucket,
    awsAccessKeyId,
    awsSecretAccessKey,
    regionName,
    azureStorageAccountName,
    azureStorageAccountKey,
    azureBlobContainerName,
    googleCredentialId,
    googleDriveFolderId,
  } = node.data;

  const value = useMemo(
    () => ({
      storageType,
      path,
      s3Bucket,
      awsAccessKeyId,
      awsSecretAccessKey,
      regionName,
      azureStorageAccountName,
      azureStorageAccountKey,
      azureBlobContainerName,
      googleCredentialId,
      googleDriveFolderId,
    }),
    [
      storageType,
      path,
      s3Bucket,
      awsAccessKeyId,
      awsSecretAccessKey,
      regionName,
      azureStorageAccountName,
      azureStorageAccountKey,
      azureBlobContainerName,
      googleCredentialId,
      googleDriveFolderId,
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

  return <FileUploadEditor blockId={blockId} />;
}

export { FileUploadBlockForm };
