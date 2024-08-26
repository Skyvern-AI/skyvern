import type { Node } from "@xyflow/react";

export type DownloadNodeData = {
  url: string;
  editable: boolean;
  label: string;
};

export type DownloadNode = Node<DownloadNodeData, "download">;
