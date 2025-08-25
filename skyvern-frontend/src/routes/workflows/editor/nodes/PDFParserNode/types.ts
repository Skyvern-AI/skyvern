import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { AppNode } from "..";
import {
  debuggableWorkflowBlockTypes,
  WorkflowModel,
} from "@/routes/workflows/types/workflowTypes";

export type PDFParserNodeData = NodeBaseData & {
  fileUrl: string;
  jsonSchema: string;
  model: WorkflowModel | null;
};

export type PDFParserNode = Node<PDFParserNodeData, "pdfParser">;

export const pdfParserNodeDefaultData: PDFParserNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("pdf_parser"),
  editable: true,
  label: "",
  fileUrl: "",
  continueOnFailure: false,
  jsonSchema: "null",
  model: null,
} as const;

export function isPdfParserNode(node: AppNode): node is PDFParserNode {
  return node.type === "pdfParser";
}
