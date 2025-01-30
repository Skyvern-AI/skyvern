import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";

export type WaitNodeData = NodeBaseData & {
  waitInSeconds: string;
};

export type WaitNode = Node<WaitNodeData, "wait">;

export const waitNodeDefaultData: WaitNodeData = {
  label: "",
  continueOnFailure: false,
  editable: true,
  waitInSeconds: "1",
};

export function isWaitNode(node: Node): node is WaitNode {
  return node.type === "wait";
}
