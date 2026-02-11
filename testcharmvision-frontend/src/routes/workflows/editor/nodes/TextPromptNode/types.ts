import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { AppNode } from "..";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export type TextPromptNodeData = NodeBaseData & {
  prompt: string;
  jsonSchema: string;
  parameterKeys: Array<string>;
};

export type TextPromptNode = Node<TextPromptNodeData, "textPrompt">;

export const textPromptNodeDefaultData: TextPromptNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("text_prompt"),
  editable: true,
  label: "",
  prompt: "",
  jsonSchema: "null",
  continueOnFailure: false,
  parameterKeys: [],
  model: null,
} as const;

export function isTextPromptNode(node: AppNode): node is TextPromptNode {
  return node.type === "textPrompt";
}
