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

export const helpTooltipContent = {
  fileUrl:
    "Since we're in beta this section isn't fully customizable yet, contact us if you'd like to integrate it into your workflow.",
} as const;
