import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
export type ValidationNodeData = NodeBaseData & {
  completeCriterion: string;
  terminateCriterion: string;
  errorCodeMapping: string;
  parameterKeys: Array<string>;
};

export type ValidationNode = Node<ValidationNodeData, "validation">;

export const validationNodeDefaultData: ValidationNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("validation"),
  label: "",
  completeCriterion: "",
  terminateCriterion: "",
  errorCodeMapping: "null",
  continueOnFailure: false,
  editable: true,
  parameterKeys: [],
  model: null,
};

export function isValidationNode(node: Node): node is ValidationNode {
  return node.type === "validation";
}
