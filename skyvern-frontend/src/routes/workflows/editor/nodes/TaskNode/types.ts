import type { Node } from "@xyflow/react";

export type TaskNodeData = {
  url: string;
  navigationGoal: string;
  dataExtractionGoal: string;
  errorCodeMapping: string;
  dataSchema: string;
  maxRetries: number | null;
  maxStepsOverride: number | null;
  allowDownloads: boolean;
  editable: boolean;
  label: string;
  parameterKeys: Array<string>;
};

export type TaskNode = Node<TaskNodeData, "task">;

export type TaskNodeDisplayMode = "basic" | "advanced";

export const taskNodeDefaultData: TaskNodeData = {
  url: "",
  navigationGoal: "",
  dataExtractionGoal: "",
  errorCodeMapping: "null",
  dataSchema: "null",
  maxRetries: null,
  maxStepsOverride: null,
  allowDownloads: false,
  editable: true,
  label: "",
  parameterKeys: [],
} as const;
