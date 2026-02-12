import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { RunEngine } from "@/api/types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export const MAX_STEPS_DEFAULT = 25;

export type NavigationNodeData = NodeBaseData & {
  url: string;
  navigationGoal: string;
  errorCodeMapping: string;
  completeCriterion: string;
  terminateCriterion: string;
  engine: RunEngine | null;
  maxRetries: number | null;
  maxStepsOverride: number | null;
  allowDownloads: boolean;
  downloadSuffix: string | null;
  parameterKeys: Array<string>;
  totpVerificationUrl: string | null;
  totpIdentifier: string | null;
  disableCache: boolean;
  includeActionHistoryInVerification: boolean;
  // V2-specific fields (used when engine is SkyvernV2)
  prompt: string;
  maxSteps: number | null;
};

export type NavigationNode = Node<NavigationNodeData, "navigation">;

export const navigationNodeDefaultData: NavigationNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("navigation"),
  label: "",
  url: "",
  navigationGoal: "",
  completeCriterion: "",
  terminateCriterion: "",
  errorCodeMapping: "null",
  model: { model_name: "" },
  engine: RunEngine.SkyvernV1,
  maxRetries: null,
  maxStepsOverride: null,
  allowDownloads: false,
  downloadSuffix: null,
  editable: true,
  parameterKeys: [],
  totpVerificationUrl: null,
  totpIdentifier: null,
  continueOnFailure: false,
  disableCache: false,
  includeActionHistoryInVerification: false,
  // V2-specific fields
  prompt: "",
  maxSteps: MAX_STEPS_DEFAULT,
} as const;

export function isNavigationNode(node: Node): node is NavigationNode {
  return node.type === "navigation";
}
