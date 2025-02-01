import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { AppNode } from "..";

export type TextPromptNodeData = NodeBaseData & {
  prompt: string;
  jsonSchema: string;
  parameterKeys: Array<string>;
};

export type TextPromptNode = Node<TextPromptNodeData, "textPrompt">;

export const textPromptNodeDefaultData: TextPromptNodeData = {
  editable: true,
  label: "",
  prompt: "",
  jsonSchema: "null",
  continueOnFailure: false,
  parameterKeys: [],
} as const;

export function isTextPromptNode(node: AppNode): node is TextPromptNode {
  return node.type === "textPrompt";
}
