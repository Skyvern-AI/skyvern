import type { Node } from "@xyflow/react";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
import { NodeBaseData } from "../types";

export type PrintPageNodeData = NodeBaseData & {
  includeTimestamp: boolean;
  customFilename: string;
  format: string;
  landscape: boolean;
  printBackground: boolean;
  parameterKeys: Array<string>;
};

export type PrintPageNode = Node<PrintPageNodeData, "printPage">;

export const printPageNodeDefaultData: PrintPageNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("print_page"),
  label: "",
  continueOnFailure: false,
  editable: true,
  model: null,
  includeTimestamp: true,
  customFilename: "",
  format: "A4",
  landscape: false,
  printBackground: true,
  parameterKeys: [],
};

export function isPrintPageNode(node: Node): node is PrintPageNode {
  return node.type === "printPage";
}
