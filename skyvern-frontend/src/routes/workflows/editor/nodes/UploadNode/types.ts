import type { Node } from "@xyflow/react";
import { SKYVERN_DOWNLOAD_DIRECTORY } from "../../constants";
import { NodeBaseData } from "../types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export type UploadNodeData = NodeBaseData & {
  path: string;
  editable: boolean;
};

export type UploadNode = Node<UploadNodeData, "upload">;

export const uploadNodeDefaultData: UploadNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("file_upload"),
  editable: true,
  label: "",
  path: SKYVERN_DOWNLOAD_DIRECTORY,
  continueOnFailure: false,
  model: null,
} as const;
