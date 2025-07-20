import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export type LoopNodeData = NodeBaseData & {
  loopValue: string;
  loopValueOrPrompt: string;  // Unified field for variable reference or prompt
  completeIfEmpty: boolean;
};

export type LoopNode = Node<LoopNodeData, "loop">;

export const loopNodeDefaultData: LoopNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("for_loop"),
  editable: true,
  label: "",
  loopValue: "",
  loopValueOrPrompt: "",  // Unified field
  completeIfEmpty: false,
  continueOnFailure: false,
  model: null,
} as const;

export function isLoopNode(node: Node): node is LoopNode {
  return node.type === "loop";
}
