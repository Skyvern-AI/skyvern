import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
export type WaitNodeData = NodeBaseData & {
  waitInSeconds: string;
};

export type WaitNode = Node<WaitNodeData, "wait">;

export const waitNodeDefaultData: WaitNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("wait"),
  label: "",
  continueOnFailure: false,
  editable: true,
  waitInSeconds: "1",
  model: null,
};

export function isWaitNode(node: Node): node is WaitNode {
  return node.type === "wait";
}
