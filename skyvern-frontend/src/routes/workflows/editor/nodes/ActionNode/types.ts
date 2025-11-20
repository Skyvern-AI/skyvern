import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { RunEngine } from "@/api/types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

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
  disableCache: boolean;
  engine: RunEngine | null;
};

export type ActionNode = Node<ActionNodeData, "action">;

export const actionNodeDefaultData: ActionNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("action"),
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
  disableCache: false,
  engine: RunEngine.SkyvernV1,
  model: null,
} as const;

export function isActionNode(node: Node): node is ActionNode {
  return node.type === "action";
}
