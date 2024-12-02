import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";

export type FileParserNodeData = NodeBaseData & {
  fileUrl: string;
};

export type FileParserNode = Node<FileParserNodeData, "fileParser">;

export const fileParserNodeDefaultData: FileParserNodeData = {
  editable: true,
  label: "",
  fileUrl: "",
  continueOnFailure: false,
} as const;
