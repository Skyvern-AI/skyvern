import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { AppNode } from "..";
import {
  debuggableWorkflowBlockTypes,
  WorkflowModel,
} from "@/routes/workflows/types/workflowTypes";

export type FileParserNodeData = NodeBaseData & {
  fileUrl: string;
  jsonSchema: string;
  model: WorkflowModel | null;
};

export type FileParserNode = Node<FileParserNodeData, "fileParser">;

export const fileParserNodeDefaultData: FileParserNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("file_url_parser"),
  editable: true,
  label: "",
  fileUrl: "",
  continueOnFailure: false,
  jsonSchema: "null",
  model: null,
} as const;

export function isFileParserNode(node: AppNode): node is FileParserNode {
  return node.type === "fileParser";
}
