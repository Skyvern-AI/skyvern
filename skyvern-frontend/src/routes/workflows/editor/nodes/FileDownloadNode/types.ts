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
} as const;

export function isFileDownloadNode(node: Node): node is FileDownloadNode {
  return node.type === "fileDownload";
}
