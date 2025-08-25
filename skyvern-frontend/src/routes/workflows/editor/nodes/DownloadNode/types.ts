import type { Node } from "@xyflow/react";
import { SKYVERN_DOWNLOAD_DIRECTORY } from "../../constants";
import { NodeBaseData } from "../types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export type DownloadNodeData = NodeBaseData & {
  url: string;
};

export type DownloadNode = Node<DownloadNodeData, "download">;

export const downloadNodeDefaultData: DownloadNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("download_to_s3"),
  editable: true,
  label: "",
  url: SKYVERN_DOWNLOAD_DIRECTORY,
  continueOnFailure: false,
  model: null,
} as const;
