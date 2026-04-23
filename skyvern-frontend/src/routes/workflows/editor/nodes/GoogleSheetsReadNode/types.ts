import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export type GoogleSheetsReadNodeData = NodeBaseData & {
  spreadsheetUrl: string;
  sheetName: string;
  range: string;
  credentialId: string;
  hasHeaderRow: boolean;
  parameterKeys: Array<string>;
};

export type GoogleSheetsReadNode = Node<
  GoogleSheetsReadNodeData,
  "googleSheetsRead"
>;

export const googleSheetsReadNodeDefaultData: GoogleSheetsReadNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("google_sheets_read"),
  label: "",
  continueOnFailure: false,
  editable: true,
  model: null,
  spreadsheetUrl: "",
  sheetName: "",
  range: "",
  credentialId: "",
  hasHeaderRow: true,
  parameterKeys: [],
};

export function isGoogleSheetsReadNode(
  node: Node,
): node is GoogleSheetsReadNode {
  return node.type === "googleSheetsRead";
}
