import type { Node } from "@xyflow/react";

export type TextPromptNodeData = {
  prompt: string;
  jsonSchema: string;
  editable: boolean;
  label: string;
};

export type TextPromptNode = Node<TextPromptNodeData, "textPrompt">;

export const textPromptNodeDefaultData: TextPromptNodeData = {
  editable: true,
  label: "",
  prompt: "",
  jsonSchema: "null",
} as const;

export const helpTooltipContent = {
  prompt:
    "Write a prompt you would like passed into the LLM and specify the output format, if applicable.",
};
