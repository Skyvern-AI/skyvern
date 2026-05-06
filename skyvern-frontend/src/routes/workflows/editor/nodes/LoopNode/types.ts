import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import {
  BranchCriteriaTypes,
  debuggableWorkflowBlockTypes,
  type BranchCriteriaType,
} from "@/routes/workflows/types/workflowTypes";

export type LoopKind = "for_each" | "while";

export type LoopNodeData = NodeBaseData & {
  loopKind: LoopKind;
  loopValue: string;
  loopVariableReference: string;
  completeIfEmpty: boolean;
  dataSchema: string;
  whileConditionExpression: string;
  whileConditionDescription: string | null;
  whileConditionCriteriaType: BranchCriteriaType;
  _headerHeight?: number; // internal: measured header card height for layout
};

export type LoopNode = Node<LoopNodeData, "loop">;

export const loopNodeDefaultData: LoopNodeData = {
  debuggable:
    debuggableWorkflowBlockTypes.has("for_loop") ||
    debuggableWorkflowBlockTypes.has("while_loop"),
  editable: true,
  label: "",
  loopKind: "for_each",
  loopValue: "",
  loopVariableReference: "",
  completeIfEmpty: false,
  continueOnFailure: false,
  nextLoopOnFailure: false,
  model: null,
  dataSchema: "null",
  whileConditionExpression: "{{ true }}",
  whileConditionDescription: null,
  whileConditionCriteriaType: BranchCriteriaTypes.Jinja2Template,
} as const;

export function isLoopNode(node: Node): node is LoopNode {
  return node.type === "loop";
}
