import type { Node } from "@xyflow/react";

export type TextPromptNodeData = {
  prompt: string;
  jsonSchema: Record<string, unknown> | null;
  editable: boolean;
  label: string;
};

export type TextPromptNode = Node<TextPromptNodeData, "textPrompt">;
