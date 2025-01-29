import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";

export type LoopNodeData = NodeBaseData & {
  loopValue: string;
  loopVariableReference: string;
  completeIfEmpty: boolean;
};

export type LoopNode = Node<LoopNodeData, "loop">;

export const loopNodeDefaultData: LoopNodeData = {
  editable: true,
  label: "",
  loopValue: "",
  loopVariableReference: "",
  completeIfEmpty: false,
  continueOnFailure: false,
} as const;

export function isLoopNode(node: Node): node is LoopNode {
  return node.type === "loop";
}
