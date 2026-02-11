import type { Node } from "@xyflow/react";

export type NodeAdderNodeData = Record<string, never>;

export type NodeAdderNode = Node<NodeAdderNodeData, "nodeAdder">;
