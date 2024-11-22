import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";

export type TaskNodeData = NodeBaseData & {
  url: string;
  navigationGoal: string;
  dataExtractionGoal: string;
  errorCodeMapping: string;
  dataSchema: string;
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

export const helpTooltipContent = {
  base: "Tell Skyvern what to do. This is the core of your task block, so make sure your prompt tells Skyvern when it has completed its task, any guardrails, and if there are cases where it should terminate the task early. Define placeholder values using the “parameters” drop down that you predefine or redefine run-to-run. This allows you to make a workflow generalizable to a variety of use cases that change with every run.",
  extraction:
    "Tell Skyvern what to extract and how it should be formatted, if applicable.",
  limits:
    "Give Skyvern limitations, such as number of retries on failure, the number of maximum steps, the option to download and append suffix identifiers, and return message for errors Skyvern encounters.",
  totp: "Link your internal TOTP storage system to relay 2FA codes we encounter straight to Skyvern. If you have multiple tasks running simultaneously, make sure to link an identifier so Skyvern knows which TOTP URL goes with which task.",
  url: "The URL Skyvern is navigating to. Leave this field blank to pick up from where the last task block left off.",
  navigationGoal:
    "Give Skyvern an objective. Make sure to include when the task is complete, when it should self-terminate, and any guardrails.",
  parameters:
    "Define placeholder values using the “parameters” drop down that you predefine or redefine run-to-run.",
  dataExtractionGoal:
    "Tell Skyvern what data you would like to scrape at the end of your run.",
  dataSchema: "Specify a format for extracted data in JSON.",
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
  url: "https://",
  navigationGoal: "Tell Skyvern what to do.",
  dataExtractionGoal: "What data do you need to extract?",
  maxRetries: "Default: 3",
  maxStepsOverride: "Default: 10",
  downloadSuffix: "Add an ID for downloaded files",
  label: "Task",
  totpVerificationUrl: "Provide your 2FA endpoint",
  totpIdentifier: "Add an ID that links your TOTP to the task",
};
