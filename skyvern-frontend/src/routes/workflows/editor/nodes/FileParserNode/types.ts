import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export type FileParserNodeData = NodeBaseData & {
  fileUrl: string;
};

export type FileParserNode = Node<FileParserNodeData, "fileParser">;

export const fileParserNodeDefaultData: FileParserNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("file_url_parser"),
  editable: true,
  label: "",
  fileUrl: "",
  continueOnFailure: false,
  model: null,
} as const;
