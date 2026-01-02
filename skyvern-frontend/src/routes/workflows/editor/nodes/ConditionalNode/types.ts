import type { Node } from "@xyflow/react";
import { nanoid } from "nanoid";

import {
  BranchCondition,
  BranchCriteria,
  BranchCriteriaTypes,
} from "@/routes/workflows/types/workflowTypes";
import { NodeBaseData } from "../types";

export type ConditionalNodeData = NodeBaseData & {
  branches: Array<BranchCondition>;
  activeBranchId: string | null;
  mergeLabel: string | null;
};

export type ConditionalNode = Node<ConditionalNodeData, "conditional">;

export const defaultBranchCriteria: BranchCriteria = {
  criteria_type: BranchCriteriaTypes.Jinja2Template,
  expression: "",
  description: null,
};

export function createBranchCondition(
  overrides: Partial<BranchCondition> = {},
): BranchCondition {
  return {
    id: overrides.id ?? nanoid(),
    criteria:
      overrides.is_default ?? false
        ? null
        : overrides.criteria
          ? {
              ...overrides.criteria,
            }
          : { ...defaultBranchCriteria },
    next_block_label: overrides.next_block_label ?? null,
    description: overrides.description ?? null,
    is_default: overrides.is_default ?? false,
  };
}

const initialBranches: Array<BranchCondition> = [
  createBranchCondition(),
  createBranchCondition({ is_default: true }),
];

export const conditionalNodeDefaultData: ConditionalNodeData = {
  debuggable: true,
  editable: true,
  label: "",
  continueOnFailure: false,
  model: null,
  showCode: false,
  branches: initialBranches,
  activeBranchId: initialBranches[0]!.id,
  mergeLabel: null,
};

export function isConditionalNode(node: Node): node is ConditionalNode {
  return node.type === "conditional";
}

export function cloneBranchConditions(
  branches: Array<BranchCondition>,
): Array<BranchCondition> {
  return branches.map((branch) =>
    createBranchCondition({
      id: branch.id,
      criteria: branch.criteria,
      next_block_label: branch.next_block_label,
      description: branch.description,
      is_default: branch.is_default,
    }),
  );
}

export function createDefaultBranchConditions(): Array<BranchCondition> {
  return [createBranchCondition(), createBranchCondition({ is_default: true })];
}
