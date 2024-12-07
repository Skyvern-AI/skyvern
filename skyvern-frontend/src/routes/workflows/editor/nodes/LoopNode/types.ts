import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";

export type LoopNodeData = NodeBaseData & {
  loopValue: string;
};

export type LoopNode = Node<LoopNodeData, "loop">;

export const loopNodeDefaultData: LoopNodeData = {
  editable: true,
  label: "",
  loopValue: "",
  continueOnFailure: false,
} as const;

export function isLoopNode(node: Node): node is LoopNode {
  return node.type === "loop";
}
