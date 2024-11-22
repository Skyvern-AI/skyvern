import type { Node } from "@xyflow/react";
import { SKYVERN_DOWNLOAD_DIRECTORY } from "../../constants";
import { NodeBaseData } from "../types";

export type DownloadNodeData = NodeBaseData & {
  url: string;
};

export type DownloadNode = Node<DownloadNodeData, "download">;

export const downloadNodeDefaultData: DownloadNodeData = {
  editable: true,
  label: "",
  url: SKYVERN_DOWNLOAD_DIRECTORY,
  continueOnFailure: false,
} as const;

export const helpTooltipContent = {
  url: "Since we're in beta this section isn't fully customizable yet, contact us if you'd like to integrate it into your workflow.",
} as const;
