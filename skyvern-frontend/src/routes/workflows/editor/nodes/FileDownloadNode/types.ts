import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { RunEngine } from "@/api/types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export type FileDownloadNodeData = NodeBaseData & {
  url: string;
  navigationGoal: string;
  errorCodeMapping: string;
  maxRetries: number | null;
  maxStepsOverride: number | null;
  downloadSuffix: string | null;
  parameterKeys: Array<string>;
  totpVerificationUrl: string | null;
  totpIdentifier: string | null;
  engine: RunEngine | null;
  disableCache: boolean;
  downloadTimeout: number | null;
  downloadTarget: "website" | "s3" | "azure" | "google_drive" | "sftp";
  path: string;
  prompt: string | null;
  s3Bucket: string | null;
  awsAccessKeyId: string | null;
  awsSecretAccessKey: string | null;
  regionName: string | null;
  azureStorageAccountName: string | null;
  azureStorageAccountKey: string | null;
  azureBlobContainerName: string | null;
  googleCredentialId: string | null;
  googleDriveFolderId: string | null;
  sftpHost: string | null;
  sftpPort: string | null;
  sftpUsername: string | null;
  sftpPassword: string | null;
  sftpPrivateKey: string | null;
  sftpPrivateKeyPassphrase: string | null;
  sftpRemotePath: string | null;
  sftpHostKey: string | null;
  continueOnEmpty: boolean;
};

export type FileDownloadNode = Node<FileDownloadNodeData, "fileDownload">;

export const fileDownloadNodeDefaultData: FileDownloadNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("file_download"),
  label: "",
  url: "",
  navigationGoal: "",
  errorCodeMapping: "null",
  maxRetries: null,
  maxStepsOverride: null,
  downloadSuffix: null,
  editable: true,
  parameterKeys: [],
  totpVerificationUrl: null,
  totpIdentifier: null,
  continueOnFailure: false,
  disableCache: false,
  engine: RunEngine.SkyvernV1,
  model: null,
  downloadTimeout: null,
  downloadTarget: "website",
  path: "{{ workflow_run_id }}",
  prompt: null,
  s3Bucket: null,
  awsAccessKeyId: null,
  awsSecretAccessKey: null,
  regionName: null,
  azureStorageAccountName: null,
  azureStorageAccountKey: null,
  azureBlobContainerName: null,
  googleCredentialId: null,
  googleDriveFolderId: null,
  sftpHost: null,
  sftpPort: null,
  sftpUsername: null,
  sftpPassword: null,
  sftpPrivateKey: null,
  sftpPrivateKeyPassphrase: null,
  sftpRemotePath: null,
  sftpHostKey: null,
  continueOnEmpty: false,
  ignoreWorkflowSystemPrompt: false,
} as const;

export function isFileDownloadNode(node: Node): node is FileDownloadNode {
  return node.type === "fileDownload";
}
