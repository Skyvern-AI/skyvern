import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { RunEngine } from "@/api/types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
export type TaskNodeData = NodeBaseData & {
  url: string;
  navigationGoal: string;
  dataExtractionGoal: string;
  errorCodeMapping: string;
  dataSchema: string;
  completeCriterion: string;
  terminateCriterion: string;
  maxRetries: number | null;
  maxStepsOverride: number | null;
  allowDownloads: boolean;
  downloadSuffix: string | null;
  parameterKeys: Array<string>;
  totpVerificationUrl: string | null;
  totpIdentifier: string | null;
  disableCache: boolean;
  includeActionHistoryInVerification: boolean;
  engine: RunEngine | null;
};

export type TaskNode = Node<TaskNodeData, "task">;

export const taskNodeDefaultData: TaskNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("task"),
  url: "",
  navigationGoal: "",
  dataExtractionGoal: "",
  errorCodeMapping: "null",
  dataSchema: "null",
  completeCriterion: "",
  terminateCriterion: "",
  maxRetries: null,
  maxStepsOverride: null,
  allowDownloads: false,
  downloadSuffix: null,
  editable: true,
  label: "",
  parameterKeys: [],
  totpVerificationUrl: null,
  totpIdentifier: null,
  continueOnFailure: false,
  disableCache: false,
  includeActionHistoryInVerification: false,
  engine: RunEngine.SkyvernV1,
  model: null,
} as const;

export function isTaskNode(node: Node): node is TaskNode {
  return node.type === "task";
}
