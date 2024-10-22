import type { Node } from "@xyflow/react";

export type StartNodeData = Record<string, never>;

export type StartNode = Node<StartNodeData, "start">;
