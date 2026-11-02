import type { Node } from "@xyflow/react";

import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

import type { NodeBaseData } from "../types";

export const POWERBI_QUERY_MAX_ROWS = 100_000;
export const POWERBI_QUERY_DEFAULT_MAX_ROWS = 1_000;

export type PowerBIQueryNodeData = NodeBaseData & {
  connectionId: string;
  datasetId: string;
  query: string;
  maxRows: number;
  parameterKeys: Array<string>;
};

export type PowerBIQueryNode = Node<PowerBIQueryNodeData, "powerBIQuery">;

export const powerBIQueryNodeDefaultData: PowerBIQueryNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("powerbi_query"),
  label: "",
  continueOnFailure: false,
  editable: true,
  model: null,
  connectionId: "",
  datasetId: "",
  query: "",
  maxRows: POWERBI_QUERY_DEFAULT_MAX_ROWS,
  parameterKeys: [],
};

export function isPowerBIQueryNode(node: Node): node is PowerBIQueryNode {
  return node.type === "powerBIQuery";
}
