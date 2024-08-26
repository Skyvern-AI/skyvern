import type { Node } from "@xyflow/react";

export type TaskNodeData = {
  url: string;
  navigationGoal: string;
  dataExtractionGoal: string;
  errorCodeMapping: Record<string, string> | null;
  dataSchema: Record<string, unknown> | null;
  maxRetries: number | null;
  maxStepsOverride: number | null;
  allowDownloads: boolean;
  editable: boolean;
  label: string;
};

export type TaskNode = Node<TaskNodeData, "task">;

export type TaskNodeDisplayMode = "basic" | "advanced";
