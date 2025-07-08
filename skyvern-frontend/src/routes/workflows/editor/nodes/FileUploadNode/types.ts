import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export type FileUploadNodeData = NodeBaseData & {
  path: string;
  editable: boolean;
  storageType: string;
  s3Bucket: string;
  awsAccessKeyId: string;
  awsSecretAccessKey: string;
  regionName: string;
};

export type FileUploadNode = Node<FileUploadNodeData, "fileUpload">;

export const fileUploadNodeDefaultData: FileUploadNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("upload_to_s3"),
  editable: true,
  storageType: "s3",
  label: "",
  path: "",
  s3Bucket: "",
  awsAccessKeyId: "",
  awsSecretAccessKey: "",
  regionName: "",
  continueOnFailure: false,
  model: null,
} as const;
