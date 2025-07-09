import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export type CodeBlockNodeData = NodeBaseData & {
  code: string;
  parameterKeys: Array<string> | null;
};

export type CodeBlockNode = Node<CodeBlockNodeData, "codeBlock">;

const codeLead = `
# This feature is currently in private beta. Please reach out to
# founders@skyvern.com to get access.
#
# Any parameter you've added to the "Input Parameters" list is available in
# global scope, by the same name.
#
# Any top-level variable you create is assigned to the output of this block.
# e.g., if you've written 'x = 5', then 'x' is included in the block output.
`;

export const codeBlockNodeDefaultData: CodeBlockNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("code"),
  editable: true,
  label: "",
  code: codeLead,
  continueOnFailure: false,
  parameterKeys: null,
  model: null,
} as const;
