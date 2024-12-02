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

export const helpTooltipContent = {
  errorCodeMapping:
    "Knowing about why a block terminated can be important, specify error messages here.",
  continueOnFailure:
    "Allow the workflow to continue if it encounters a failure.",
} as const;

export function isValidationNode(node: Node): node is ValidationNode {
  return node.type === "validation";
}
