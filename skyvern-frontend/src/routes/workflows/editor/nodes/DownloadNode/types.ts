import type { Node } from "@xyflow/react";

export type DownloadNodeData = {
  url: string;
  editable: boolean;
  label: string;
};

export type DownloadNode = Node<DownloadNodeData, "download">;

export const downloadNodeDefaultData: DownloadNodeData = {
  editable: true,
  label: "",
  url: "SKYVERN_DOWNLOAD_DIRECTORY",
} as const;
