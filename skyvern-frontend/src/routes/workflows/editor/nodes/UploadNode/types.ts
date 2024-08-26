import type { Node } from "@xyflow/react";

export type UploadNodeData = {
  path: string;
  editable: boolean;
  label: string;
};

export type UploadNode = Node<UploadNodeData, "upload">;
