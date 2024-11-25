import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";

export type NavigationNodeData = NodeBaseData & {
  url: string;
  navigationGoal: string;
  errorCodeMapping: string;
  maxRetries: number | null;
  maxStepsOverride: number | null;
  allowDownloads: boolean;
  downloadSuffix: string | null;
  parameterKeys: Array<string>;
  totpVerificationUrl: string | null;
  totpIdentifier: string | null;
  cacheActions: boolean;
};

export type NavigationNode = Node<NavigationNodeData, "navigation">;

export const navigationNodeDefaultData: NavigationNodeData = {
  label: "",
  url: "",
  navigationGoal: "",
  errorCodeMapping: "null",
  maxRetries: null,
  maxStepsOverride: null,
  allowDownloads: false,
  downloadSuffix: null,
  editable: true,
  parameterKeys: [],
  totpVerificationUrl: null,
  totpIdentifier: null,
  continueOnFailure: false,
  cacheActions: false,
} as const;

export function isNavigationNode(node: Node): node is NavigationNode {
  return node.type === "navigation";
}
