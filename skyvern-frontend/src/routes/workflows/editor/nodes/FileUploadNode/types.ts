import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export type FileUploadNodeData = NodeBaseData & {
  path: string;
  editable: boolean;
  storageType: "s3" | "azure";
  s3Bucket: string | null;
  awsAccessKeyId: string | null;
  awsSecretAccessKey: string | null;
  regionName: string | null;
  azureStorageAccountName: string | null;
  azureStorageAccountKey: string | null;
  azureBlobContainerName: string | null;
};

export type FileUploadNode = Node<FileUploadNodeData, "fileUpload">;

export const fileUploadNodeDefaultData: FileUploadNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("upload_to_s3"),
  editable: true,
  storageType: "s3",
  label: "",
  path: "{{ workflow_run_id }}",
  s3Bucket: null,
  awsAccessKeyId: null,
  awsSecretAccessKey: null,
  regionName: null,
  azureStorageAccountName: null,
  azureStorageAccountKey: null,
  azureBlobContainerName: null,
  continueOnFailure: false,
  model: null,
} as const;
