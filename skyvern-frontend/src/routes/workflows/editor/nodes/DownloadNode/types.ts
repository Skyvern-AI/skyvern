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
