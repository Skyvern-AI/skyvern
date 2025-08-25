import { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export type URLNodeData = NodeBaseData & {
  url: string;
};

export type URLNode = Node<URLNodeData, "url">;

export const urlNodeDefaultData: URLNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("goto_url"),
  label: "",
  continueOnFailure: false,
  url: "",
  editable: true,
  model: null,
};

export function isUrlNode(node: Node): node is URLNode {
  return node.type === "url";
}
