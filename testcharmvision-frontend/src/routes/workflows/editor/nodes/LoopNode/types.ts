import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export type LoopNodeData = NodeBaseData & {
  loopValue: string;
  loopVariableReference: string;
  completeIfEmpty: boolean;
  continueOnFailure: boolean;
  nextLoopOnFailure?: boolean;
};

export type LoopNode = Node<LoopNodeData, "loop">;

export const loopNodeDefaultData: LoopNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("for_loop"),
  editable: true,
  label: "",
  loopValue: "",
  loopVariableReference: "",
  completeIfEmpty: false,
  continueOnFailure: false,
  nextLoopOnFailure: false,
  model: null,
} as const;

export function isLoopNode(node: Node): node is LoopNode {
  return node.type === "loop";
}
