import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";

export type ValidationNodeData = NodeBaseData & {
  completeCriterion: string;
  terminateCriterion: string;
  errorCodeMapping: string;
  parameterKeys: Array<string>;
};

export type ValidationNode = Node<ValidationNodeData, "validation">;

export const validationNodeDefaultData: ValidationNodeData = {
  label: "",
  completeCriterion: "",
  terminateCriterion: "",
  errorCodeMapping: "null",
  continueOnFailure: false,
  editable: true,
  parameterKeys: [],
};

export function isValidationNode(node: Node): node is ValidationNode {
  return node.type === "validation";
}
