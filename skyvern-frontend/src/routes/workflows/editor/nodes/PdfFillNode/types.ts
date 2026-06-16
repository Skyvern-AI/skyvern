import type { Node } from "@xyflow/react";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
import { NodeBaseData } from "../types";

export type PdfFillNodeData = NodeBaseData & {
  fileUrl: string;
  prompt: string;
  payload: string;
  llmKey: string;
  parameterKeys: Array<string>;
};

export type PdfFillNode = Node<PdfFillNodeData, "pdfFill">;

export const pdfFillNodeDefaultData: PdfFillNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("pdf_fill"),
  label: "",
  continueOnFailure: false,
  editable: true,
  model: null,
  fileUrl: "",
  prompt: "",
  payload: "{}",
  llmKey: "",
  parameterKeys: [],
};

export function isPdfFillNode(node: Node): node is PdfFillNode {
  return node.type === "pdfFill";
}
