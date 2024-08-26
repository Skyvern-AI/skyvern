import type { Node } from "@xyflow/react";

export type FileParserNodeData = {
  fileUrl: string;
  editable: boolean;
  label: string;
};

export type FileParserNode = Node<FileParserNodeData, "fileParser">;
