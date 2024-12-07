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
