import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";

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
  cacheActions: boolean;
};

export type FileDownloadNode = Node<FileDownloadNodeData, "fileDownload">;

export const fileDownloadNodeDefaultData: FileDownloadNodeData = {
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
  cacheActions: false,
} as const;

export function isFileDownloadNode(node: Node): node is FileDownloadNode {
  return node.type === "fileDownload";
}
