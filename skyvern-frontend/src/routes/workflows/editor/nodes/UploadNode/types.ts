import type { Node } from "@xyflow/react";

export type UploadNodeData = {
  path: string;
  editable: boolean;
  label: string;
};

export type UploadNode = Node<UploadNodeData, "upload">;

export const uploadNodeDefaultData: UploadNodeData = {
  editable: true,
  label: "",
  path: "SKYVERN_DOWNLOAD_DIRECTORY",
} as const;
