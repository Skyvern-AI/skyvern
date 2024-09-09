import type { Node } from "@xyflow/react";

export type FileParserNodeData = {
  fileUrl: string;
  editable: boolean;
  label: string;
};

export type FileParserNode = Node<FileParserNodeData, "fileParser">;

export const fileParserNodeDefaultData: FileParserNodeData = {
  editable: true,
  label: "",
  fileUrl: "",
} as const;
