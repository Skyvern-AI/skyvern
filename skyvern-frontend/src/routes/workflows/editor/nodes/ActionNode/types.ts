import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";

export type ActionNodeData = NodeBaseData & {
  url: string;
  navigationGoal: string;
  errorCodeMapping: string;
  maxRetries: number | null;
  allowDownloads: boolean;
  downloadSuffix: string | null;
  parameterKeys: Array<string>;
  totpVerificationUrl: string | null;
  totpIdentifier: string | null;
  cacheActions: boolean;
};

export type ActionNode = Node<ActionNodeData, "action">;

export const actionNodeDefaultData: ActionNodeData = {
  label: "",
  url: "",
  navigationGoal: "",
  errorCodeMapping: "null",
  maxRetries: null,
  allowDownloads: false,
  downloadSuffix: null,
  editable: true,
  parameterKeys: [],
  totpVerificationUrl: null,
  totpIdentifier: null,
  continueOnFailure: false,
  cacheActions: false,
} as const;

export function isActionNode(node: Node): node is ActionNode {
  return node.type === "action";
}
