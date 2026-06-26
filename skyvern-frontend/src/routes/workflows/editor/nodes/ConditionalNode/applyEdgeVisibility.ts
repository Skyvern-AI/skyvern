import type { Edge } from "@xyflow/react";

type BranchMeta = {
  conditionalNodeId?: string | null;
  conditionalBranchId?: string | null;
};

type VisNode = {
  id: string;
  hidden?: boolean;
  type?: string;
  data?: Record<string, unknown>;
};

/**
 * Priority: branch-affinity match > hidden-node propagation > both-visible non-branch edges.
 */
export function applyEdgeVisibility(
  edge: Edge,
  nodeMap: Map<string, VisNode>,
  conditionalId: string,
  activeBranchId: string,
): Edge {
  const edgeData = edge.data as BranchMeta | undefined;

  if (
    edgeData?.conditionalNodeId === conditionalId &&
    edgeData?.conditionalBranchId
  ) {
    const shouldHide = edgeData.conditionalBranchId !== activeBranchId;
    return { ...edge, hidden: shouldHide };
  }

  const sourceNode = nodeMap.get(edge.source);
  const targetNode = nodeMap.get(edge.target);

  if (sourceNode?.hidden || targetNode?.hidden) {
    return { ...edge, hidden: true };
  }

  if (sourceNode && targetNode && !sourceNode.hidden && !targetNode.hidden) {
    const isConditionalBranchEdge =
      edgeData?.conditionalNodeId && edgeData?.conditionalBranchId;
    if (!isConditionalBranchEdge) {
      const srcBranch =
        sourceNode.data?.conditionalNodeId === conditionalId
          ? sourceNode.data?.conditionalBranchId
          : null;
      const tgtBranch =
        targetNode.data?.conditionalNodeId === conditionalId
          ? targetNode.data?.conditionalBranchId
          : null;
      const srcInactive = srcBranch != null && srcBranch !== activeBranchId;
      const tgtInactive = tgtBranch != null && tgtBranch !== activeBranchId;
      if (!srcInactive && !tgtInactive) {
        return { ...edge, hidden: false };
      }
    }
  }

  return edge;
}
