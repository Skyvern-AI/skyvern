import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";

export type CodeBlockNodeData = NodeBaseData & {
  code: string;
};

export type CodeBlockNode = Node<CodeBlockNodeData, "codeBlock">;

export const codeBlockNodeDefaultData: CodeBlockNodeData = {
  editable: true,
  label: "",
  code: `# All variables will be assigned to the output of this block.\n# Like 'x = 5', 'x' will be assigned to the output of this block.\n# This feature is currently in private beta. Please reach out to founders@skyvern.com to get access\n\n`,
  continueOnFailure: false,
} as const;
