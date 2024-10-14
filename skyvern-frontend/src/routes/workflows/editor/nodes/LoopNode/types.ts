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

export function isLoopNode(node: Node): node is LoopNode {
  return node.type === "loop";
}

export const helpTooltipContent = {
  loopValue:
    "Define this parameterized field with a parameter key to let Skyvern know the core value you're iterating over.",
} as const;
