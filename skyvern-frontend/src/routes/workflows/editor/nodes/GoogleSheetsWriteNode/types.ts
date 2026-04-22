import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export type GoogleSheetsWriteNodeData = NodeBaseData & {
  spreadsheetUrl: string;
  sheetName: string;
  range: string;
  credentialId: string;
  writeMode: "append" | "update";
  values: string;
  columnMapping: string;
  createSheetIfMissing: boolean;
  parameterKeys: Array<string>;
};

export type GoogleSheetsWriteNode = Node<
  GoogleSheetsWriteNodeData,
  "googleSheetsWrite"
>;

export const googleSheetsWriteNodeDefaultData: GoogleSheetsWriteNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("google_sheets_write"),
  label: "",
  continueOnFailure: false,
  editable: true,
  model: null,
  spreadsheetUrl: "",
  sheetName: "",
  range: "",
  credentialId: "",
  writeMode: "append",
  values: "",
  columnMapping: "",
  createSheetIfMissing: false,
  parameterKeys: [],
};

export function isGoogleSheetsWriteNode(
  node: Node,
): node is GoogleSheetsWriteNode {
  return node.type === "googleSheetsWrite";
}
