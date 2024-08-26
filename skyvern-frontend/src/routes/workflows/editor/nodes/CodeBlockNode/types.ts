import type { Node } from "@xyflow/react";

export type CodeBlockNodeData = {
  code: string;
  editable: boolean;
  label: string;
};

export type CodeBlockNode = Node<CodeBlockNodeData, "codeBlock">;
