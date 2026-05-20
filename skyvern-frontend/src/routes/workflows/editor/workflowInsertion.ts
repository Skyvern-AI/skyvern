import type { Edge } from "@xyflow/react";

import type { BranchContext } from "@/store/WorkflowPanelStore";

import type { NodeBaseData } from "./nodes/types";

type InsertionEdgeContext = {
  branch?: BranchContext;
  next: string | null;
  previous: string | null;
};

type InsertionNode = {
  id: string;
  parentId?: string;
  type?: string;
  data?: unknown;
};

function branchContextFromNodeData(
  data: Partial<NodeBaseData> | undefined,
): BranchContext | undefined {
  if (!data?.conditionalBranchId || !data.conditionalNodeId) {
    return undefined;
  }

  return {
    conditionalNodeId: data.conditionalNodeId,
    conditionalLabel:
      data.conditionalLabel ?? data.label ?? data.conditionalNodeId,
    branchId: data.conditionalBranchId,
    mergeLabel: data.conditionalMergeLabel ?? null,
  };
}

function branchContextFromConditionalNode(
  node: InsertionNode | undefined,
): BranchContext | undefined {
  if (
    node?.type !== "conditional" ||
    !node.data ||
    typeof node.data !== "object"
  ) {
    return undefined;
  }

  const conditionalData = node.data as {
    activeBranchId: string | null;
    branches: Array<{ id: string }>;
    label: string;
    mergeLabel: string | null;
  };
  const activeBranch = conditionalData.branches?.find(
    (branch) => branch.id === conditionalData.activeBranchId,
  );

  if (!activeBranch) {
    return undefined;
  }

  return {
    conditionalNodeId: node.id,
    conditionalLabel: conditionalData.label,
    branchId: activeBranch.id,
    mergeLabel: conditionalData.mergeLabel ?? null,
  };
}

export function findBranchContextForInsertion(
  nodes: Array<InsertionNode>,
  nodeId: string | undefined,
  parentId?: string,
): BranchContext | undefined {
  const nodeById = new Map(nodes.map((node) => [node.id, node]));

  const findFromNode = (
    startId: string | undefined,
    { includeStartingConditional }: { includeStartingConditional: boolean },
  ) => {
    const visited = new Set<string>();
    let currentId = startId;
    let isStartingNode = true;

    while (currentId && !visited.has(currentId)) {
      visited.add(currentId);

      const node = nodeById.get(currentId);
      if (!node) {
        return undefined;
      }

      const nodeBranchContext = branchContextFromNodeData(
        node.data as Partial<NodeBaseData> | undefined,
      );
      if (nodeBranchContext) {
        return nodeBranchContext;
      }

      const conditionalBranchContext =
        (includeStartingConditional || !isStartingNode) &&
        branchContextFromConditionalNode(node);
      if (conditionalBranchContext) {
        return conditionalBranchContext;
      }

      isStartingNode = false;
      currentId = node.parentId;
    }

    return undefined;
  };

  return (
    findFromNode(nodeId, { includeStartingConditional: false }) ??
    findFromNode(parentId, { includeStartingConditional: true })
  );
}

export function shouldKeepExistingEdgeForInsertion(
  edge: Edge,
  { branch, next, previous }: InsertionEdgeContext,
): boolean {
  if (!previous || edge.source !== previous) {
    return true;
  }

  // Only replace the selected insertion edge; preserve any other outgoing
  // edges from the same node, such as inactive conditional branch edges.
  if (next && edge.target !== next) {
    return true;
  }

  if (!branch) {
    return false;
  }

  const edgeData = edge.data as { conditionalBranchId?: string } | undefined;

  return Boolean(
    edgeData?.conditionalBranchId &&
    edgeData.conditionalBranchId !== branch.branchId,
  );
}
