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
  downloadSuffix: string | null;
  editable: boolean;
  label: string;
  parameterKeys: Array<string>;
  totpVerificationUrl: string | null;
  totpIdentifier: string | null;
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
  downloadSuffix: null,
  editable: true,
  label: "",
  parameterKeys: [],
  totpVerificationUrl: null,
  totpIdentifier: null,
} as const;

export function isTaskNode(node: Node): node is TaskNode {
  return node.type === "task";
}
export const helpTooltipContent = {
  base: "Tell Skyvern what to do. This is the core of your task block, so make sure your prompt tells Skyvern when it has completed its task, any guardrails, and if there are cases where it should terminate the task early. Define placeholder values using the “parameters” drop down that you predefine or redefine run-to-run. This allows you to make a workflow generalizable to a variety of use cases that change with every run.",
  extraction:
    "Tell Skyvern what to extract and how it should be formatted, if applicable.",
  limits:
    "Give Skyvern limitations, such as number of retries on failure, the number of maximum steps, the option to download and append suffix identifiers, and return message for errors Skyvern encounters.",
  totp: "Link your internal TOTP storage system to relay 2FA codes we encounter straight to Skyvern. If you have multiple tasks running simultaneously, make sure to link an identifier so Skyvern knows which TOTP URL goes with which task.",
};
