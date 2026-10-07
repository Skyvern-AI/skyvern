import { type AppNode, isWorkflowBlockNode } from "../nodes";
import {
  type ConditionalNode,
  isConditionalNode,
} from "../nodes/ConditionalNode/types";

// A container ancestor of a run-blocking block, rendered as a breadcrumb so
// users can see where a nested block lives before locating it.
export type RunBlockingPathSegment = {
  kind: "loop" | "conditional";
  label: string;
  // Only set for conditional segments: which branch the descendant sits on.
  branch?: string;
};

export type RunBlockingLocation = {
  // Loop/conditional container node ids on the path to the block, used to roll
  // a descendant count up onto collapsed ancestors.
  ancestorIds: Array<string>;
  // Outermost-first breadcrumb of container ancestors (excludes the block).
  path: Array<RunBlockingPathSegment>;
};

function branchLabel(
  conditional: ConditionalNode,
  branchId: string | null | undefined,
): string | undefined {
  if (!branchId) {
    return undefined;
  }
  const branches = conditional.data.branches;
  const index = branches.findIndex((branch) => branch.id === branchId);
  const branch = branches[index];
  if (!branch) {
    return undefined;
  }
  if (branch.is_default) {
    return "else";
  }
  const described = branch.description?.trim();
  return described && described.length > 0 ? described : `branch ${index + 1}`;
}

// Walks parentId from `nodeId` to the root, collecting loop/conditional
// container ancestors. Conditional-branch membership rides on the child's
// `conditionalBranchId`, so the branch label is resolved from the child at the
// hop into its conditional parent.
export function resolveBlockLocation(
  byId: Map<string, AppNode>,
  nodeId: string,
): RunBlockingLocation {
  const ancestorIds: Array<string> = [];
  const segments: Array<RunBlockingPathSegment> = [];
  const visited = new Set<string>();

  let current = byId.get(nodeId);
  while (current && current.parentId && !visited.has(current.id)) {
    visited.add(current.id);
    const parent = byId.get(current.parentId);
    // visited.has(parent.id) closes a multi-node cycle: the loop condition
    // only catches it after the repeated ancestor has already been collected.
    if (!parent || visited.has(parent.id)) {
      break;
    }

    if (parent.type === "loop") {
      ancestorIds.push(parent.id);
      const label = isWorkflowBlockNode(parent) ? parent.data.label : "";
      if (label) {
        segments.push({ kind: "loop", label });
      }
    } else if (isConditionalNode(parent)) {
      ancestorIds.push(parent.id);
      const branch =
        isWorkflowBlockNode(current) && "conditionalBranchId" in current.data
          ? branchLabel(parent, current.data.conditionalBranchId)
          : undefined;
      segments.push({ kind: "conditional", label: parent.data.label, branch });
    }

    current = parent;
  }

  return { ancestorIds, path: segments.reverse() };
}
