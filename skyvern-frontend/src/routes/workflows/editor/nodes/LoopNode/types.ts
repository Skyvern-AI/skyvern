import type { Node } from "@xyflow/react";

export type LoopNodeData = {
  loopValue: string;
  editable: boolean;
  label: string;
};

export type LoopNode = Node<LoopNodeData, "loop">;

export const loopNodeDefaultData: LoopNodeData = {
  editable: true,
  label: "",
  loopValue: "",
} as const;
