import type { Node } from "@xyflow/react";
import { SKYVERN_DOWNLOAD_DIRECTORY } from "../../constants";
import { NodeBaseData } from "../types";

export type UploadNodeData = NodeBaseData & {
  path: string;
  editable: boolean;
};

export type UploadNode = Node<UploadNodeData, "upload">;

export const uploadNodeDefaultData: UploadNodeData = {
  editable: true,
  label: "",
  path: SKYVERN_DOWNLOAD_DIRECTORY,
  continueOnFailure: false,
} as const;

export const helpTooltipContent = {
  path: "Since we're in beta this section isn't fully customizable yet, contact us if you'd like to integrate it into your workflow.",
} as const;
