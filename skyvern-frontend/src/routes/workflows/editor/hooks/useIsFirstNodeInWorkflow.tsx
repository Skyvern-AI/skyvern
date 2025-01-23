import { Edge, useEdges, useNodes } from "@xyflow/react";
import { AppNode } from "../nodes";

function isFirstNode(nodes: Array<AppNode>, edges: Array<Edge>, id: string) {
  const node = nodes.find((node) => node.id === id);
  if (!node) {
    return false; // doesn't make sense but for TS
  }
  const incomingEdge = edges.find((edge) => edge.target === node.id);
  if (!incomingEdge) {
    return false;
  }
  const source = incomingEdge.source;
  const sourceNode = nodes.find((node) => node.id === source);
  if (!sourceNode) {
    return false;
  }
  return !node.parentId && sourceNode.type === "start";
}

type Props = {
  id: string;
};

function useIsFirstBlockInWorkflow({ id }: Props): boolean {
  const nodes = useNodes<AppNode>();
  const edges = useEdges();

  return isFirstNode(nodes, edges, id);
}

export { useIsFirstBlockInWorkflow };
