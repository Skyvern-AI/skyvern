import type { Node } from "@xyflow/react";

import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

import { NodeBaseData } from "../types";

export type TerminateNodeData = NodeBaseData & {
  reason: string;
};

export type TerminateNode = Node<TerminateNodeData, "terminate">;

export const terminateNodeDefaultData: TerminateNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("terminate"),
  editable: true,
  label: "",
  reason: "",
  continueOnFailure: false,
  model: null,
} as const;

export function isTerminateNode(node: Node): node is TerminateNode {
  return node.type === "terminate";
}
