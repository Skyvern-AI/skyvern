import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { AppNode } from "..";

export type PDFParserNodeData = NodeBaseData & {
  fileUrl: string;
  jsonSchema: string;
};

export type PDFParserNode = Node<PDFParserNodeData, "pdfParser">;

export const pdfParserNodeDefaultData: PDFParserNodeData = {
  editable: true,
  label: "",
  fileUrl: "",
  continueOnFailure: false,
  jsonSchema: "null",
} as const;

export function isPdfParserNode(node: AppNode): node is PDFParserNode {
  return node.type === "pdfParser";
}
