import type { Node } from "@xyflow/react";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
import { NodeBaseData } from "../types";

export type SplitPdfNodeData = NodeBaseData & {
  fileUrl: string;
  prompt: string;
  llmKey: string;
  parameterKeys: Array<string>;
};

export type SplitPdfNode = Node<SplitPdfNodeData, "splitPdf">;

export const splitPdfNodeDefaultData: SplitPdfNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("split_pdf"),
  label: "",
  continueOnFailure: false,
  editable: true,
  model: null,
  fileUrl: "",
  prompt: "",
  llmKey: "",
  parameterKeys: [],
};

export function isSplitPdfNode(node: Node): node is SplitPdfNode {
  return node.type === "splitPdf";
}
