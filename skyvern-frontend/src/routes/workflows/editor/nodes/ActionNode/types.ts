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

export const helpTooltipContent = {
  navigationGoal:
    "Specify a single step or action you'd like Skyvern to complete. Actions are one-off tasks like filling a field or interacting with a specific element on the page.\n\nCurrently supported actions are click, input text, upload file, and select.",
  maxRetries:
    "Specify how many times you would like a task to retry upon failure.",
  maxStepsOverride:
    "Specify the maximum number of steps a task can take in total.",
  completeOnDownload:
    "Allow Skyvern to auto-complete the task when it downloads a file.",
  fileSuffix:
    "A file suffix that's automatically added to all downloaded files.",
  errorCodeMapping:
    "Knowing about why a task terminated can be important, specify error messages here.",
  totpVerificationUrl:
    "If you have an internal system for storing TOTP codes, link the endpoint here.",
  totpIdentifier:
    "If you are running multiple tasks or workflows at once, you will need to give the task an identifier to know that this TOTP goes with this task.",
  continueOnFailure:
    "Allow the workflow to continue if it encounters a failure.",
  cacheActions: "Cache the actions of this task.",
} as const;

export const fieldPlaceholders = {
  navigationGoal: 'Input text into "Name" field.',
  maxRetries: "Default: 3",
  maxStepsOverride: "Default: 10",
  downloadSuffix: "Add an ID for downloaded files",
  totpVerificationUrl: "Provide your 2FA endpoint",
  totpIdentifier: "Add an ID that links your TOTP to the task",
};
