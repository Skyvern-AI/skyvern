import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";

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
  cacheActions: boolean;
};

export type TaskNode = Node<TaskNodeData, "task">;

export const taskNodeDefaultData: TaskNodeData = {
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
  cacheActions: false,
} as const;

export function isTaskNode(node: Node): node is TaskNode {
  return node.type === "task";
}
