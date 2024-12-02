import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";

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

export const helpTooltipContent = {
  prompt:
    "Write a prompt you would like passed into the LLM and specify the output format, if applicable.",
};
