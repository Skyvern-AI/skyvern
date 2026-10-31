import type { Node } from "@xyflow/react";
import { RunEngine } from "@/api/types";
import {
  debuggableWorkflowBlockTypes,
  type WorkflowBlockType,
} from "../../types/workflowTypes";
import { blockTypeFromNode } from "../nodes/blockTypeFromNode";
import { stripJinjaReferences } from "./docTextUtils";

export type DocTagColor = "blue" | "green" | "purple" | "orange" | "brown";

// Tag colours from the Doc editor design; the dark values keep the same hue on slate.
export const docTagClasses: Record<DocTagColor, string> = {
  blue: "bg-[#E7F1F8] text-[#2F76A6] dark:bg-[rgba(106,172,218,.15)] dark:text-[#6AACDA]",
  green:
    "bg-[#EAF3EC] text-[#3F7A57] dark:bg-[rgba(108,191,140,.14)] dark:text-[#6CBF8C]",
  purple:
    "bg-[#F3EEF8] text-[#8456A8] dark:bg-[rgba(183,147,218,.15)] dark:text-[#B793DA]",
  orange:
    "bg-[#FBEEE2] text-[#B45B0A] dark:bg-[rgba(231,154,85,.15)] dark:text-[#E79A55]",
  brown:
    "bg-[#F4EEEB] text-[#8E6149] dark:bg-[rgba(197,154,128,.14)] dark:text-[#C59A80]",
};

const tagColors: Record<WorkflowBlockType, DocTagColor> = {
  task: "blue",
  task_v2: "blue",
  agent_step: "blue",
  navigation: "blue",
  action: "blue",
  login: "blue",
  validation: "blue",
  goto_url: "blue",
  print_page: "blue",
  extraction: "green",
  text_prompt: "green",
  file_url_parser: "green",
  pdf_parser: "green",
  web_search: "green",
  http_request: "green",
  code: "green",
  data_export: "green",
  powerbi_query: "green",
  for_loop: "purple",
  while_loop: "purple",
  conditional: "purple",
  wait: "purple",
  terminate: "purple",
  workflow_trigger: "purple",
  human_interaction: "orange",
  send_email: "orange",
  email_inbox: "orange",
  google_sheets_read: "orange",
  google_sheets_write: "orange",
  file_download: "brown",
  download_to_s3: "brown",
  upload_to_s3: "brown",
  file_upload: "brown",
  pdf_fill: "brown",
  split_pdf: "brown",
};

export function docBlockType(node: Node): WorkflowBlockType | null {
  if (node.type === "dataExport") return "data_export";
  return blockTypeFromNode(node) as WorkflowBlockType | null;
}

export function docStepCanRun(node: Node): boolean {
  const blockType = docBlockType(node);
  return !!blockType && debuggableWorkflowBlockTypes.has(blockType);
}

export function docTagColor(blockType: WorkflowBlockType | null): DocTagColor {
  return (blockType && tagColors[blockType]) ?? "blue";
}

export type DocProseField = { key: string; label: string };
const field = (key: string, label: string): DocProseField => ({ key, label });

// Prompt-driven steps read as their instruction. The first non-empty field is the
// step's text; later ones are shown under it with their label.
const proseFields: Record<string, DocProseField[]> = {
  task: [
    field("navigationGoal", "Goal"),
    field("dataExtractionGoal", "Extract"),
    field("completeCriterion", "Complete if"),
  ],
  taskv2: [field("prompt", "Prompt")],
  action: [field("navigationGoal", "Action")],
  extraction: [field("dataExtractionGoal", "Extract")],
  login: [
    field("navigationGoal", "Login goal"),
    field("completeCriterion", "Complete if"),
  ],
  validation: [
    field("completeCriterion", "Complete if"),
    field("terminateCriterion", "Terminate if"),
  ],
  human_interaction: [field("instructions", "Instructions")],
  textPrompt: [field("prompt", "Prompt")],
  agentStep: [field("prompt", "Prompt")],
  fileDownload: [field("navigationGoal", "Download goal")],
};

export function getDocProseFields(node: Node): DocProseField[] {
  // "Complete if" isn't saved for 2.0 navigation, so it isn't shown there.
  if (node.type === "navigation")
    return node.data.engine === RunEngine.SkyvernV2
      ? [field("prompt", "Prompt")]
      : [
          field("navigationGoal", "Goal"),
          field("completeCriterion", "Complete if"),
        ];
  return proseFields[node.type ?? ""] ?? [];
}

const text = (node: Node, key: string) =>
  typeof node.data[key] === "string" ? (node.data[key] as string).trim() : "";

export function getDocStepSummary(node: Node): string {
  switch (node.type) {
    case "url":
      return text(node, "url") ? `Go to ${text(node, "url")}` : "Go to a URL";
    case "wait": {
      const seconds = node.data.waitInSeconds ?? 0;
      return `Wait ${seconds} ${String(seconds) === "1" ? "second" : "seconds"}`;
    }
    case "sendEmail":
      return text(node, "subject")
        ? `Send an email: ${text(node, "subject")}`
        : "Send an email";
    case "http_request":
      return `Call ${text(node, "method") || "GET"} ${text(node, "url") || "an API"}`;
    case "codeBlock":
      return text(node, "prompt") || "Run Python code";
    case "fileParser":
      return "Read data from a file";
    case "pdfParser":
      return "Read data from a PDF";
    case "upload":
      return "Upload files to S3";
    case "fileUpload":
      return "Upload files to storage";
    case "download":
      return "Download a file from S3";
    case "printPage":
      return "Save the current page as a PDF";
    case "workflowTrigger":
      return text(node, "workflowTitle")
        ? `Run ${text(node, "workflowTitle")}`
        : "Run another agent";
    case "googleSheetsRead":
      return "Read rows from Google Sheets";
    case "googleSheetsWrite":
      return "Write rows to Google Sheets";
    case "dataExport":
      return "Export data to a file";
    case "web_search":
      return text(node, "query")
        ? `Search the web for ${text(node, "query")}`
        : "Search the web";
    case "emailInbox":
      return text(node, "prompt") || "Find an email";
    case "terminate":
      return text(node, "reason") ? `Stop: ${text(node, "reason")}` : "Stop";
    case "pdfFill":
      return text(node, "prompt") || "Fill a PDF form";
    case "splitPdf":
      return text(node, "prompt") || "Split a PDF";
    case "loop":
      return node.data.loopKind === "while"
        ? `Repeat while ${text(node, "whileConditionExpression") || "a condition holds"}`
        : `For each ${text(node, "loopVariableReference") || text(node, "loopValue") || "item"}`;
    default:
      return "No instructions yet";
  }
}

// Steps whose summary line is a setting (a loop condition, a stop reason, a wait), not a fixed phrase.
const SUMMARY_IS_SETTING = new Set([
  "loop",
  "terminate",
  "wait",
  "codeBlock",
  "emailInbox",
  "pdfFill",
  "splitPdf",
  "web_search",
]);

/** The summary to show under a step's description, so the description never hides the setting. */
export function getDocSettingSummary(node: Node): string | null {
  return SUMMARY_IS_SETTING.has(node.type ?? "")
    ? getDocStepSummary(node)
    : null;
}

export type DocKeyField = { key: string; label: string; value: string };

const keyFields: Record<string, Array<[string, string]>> = {
  url: [["url", "URL"]],
  http_request: [
    ["method", "Method"],
    ["url", "URL"],
  ],
  sendEmail: [
    ["recipients", "Recipients"],
    ["subject", "Subject"],
  ],
  fileParser: [
    ["fileUrl", "File"],
    ["fileType", "Type"],
  ],
  pdfParser: [["fileUrl", "File"]],
  workflowTrigger: [["workflowTitle", "Agent"]],
  googleSheetsRead: [
    ["spreadsheetUrl", "Spreadsheet"],
    ["range", "Range"],
  ],
  googleSheetsWrite: [
    ["spreadsheetUrl", "Spreadsheet"],
    ["range", "Range"],
  ],
};

export function getDocKeyFields(node: Node): DocKeyField[] {
  return (keyFields[node.type ?? ""] ?? []).flatMap(([key, label]) => {
    const value = node.data[key];
    const shown = Array.isArray(value)
      ? value.filter((v) => typeof v === "string" && v).join(", ")
      : typeof value === "string" || typeof value === "number"
        ? String(value).trim()
        : "";
    return shown ? [{ key, label, value: shown }] : [];
  });
}

/** One line naming a step, for the outline and Jump buttons. */
export function docStepTitle(node: Node): string {
  if (node.type === "conditional") {
    const branches = Array.isArray(node.data.branches)
      ? (node.data.branches as Array<{
          is_default?: boolean;
          criteria?: { expression?: string } | null;
        }>)
      : [];
    const first = branches.find(
      (b) => !b.is_default && b.criteria?.expression?.trim(),
    );
    return clip(
      first ? `If ${first.criteria!.expression!.trim()}` : "Conditional",
    );
  }
  const description =
    typeof node.data.description === "string" ? node.data.description : "";
  const prose = getDocProseFields(node)
    .map((f) => text(node, f.key))
    .find(Boolean);
  const title =
    prose ||
    description.trim() ||
    (getDocProseFields(node).length
      ? String(node.data.label)
      : getDocStepSummary(node));
  return clip(title);
}

function clip(title: string): string {
  const plain = stripJinjaReferences(title);
  return plain.length > 60 ? `${plain.slice(0, 59)}…` : plain;
}

/** Ranks "Add a step" choices: a title match beats a word in a description. */
export function docStepFilter(
  title: string,
  search: string,
  keywords: string[] = [],
): number {
  const q = search.trim().toLowerCase();
  if (!q) return 1;
  const t = title.toLowerCase().replace(/ block$/, "");
  if (t === q) return 1;
  if (t.startsWith(q)) return 0.9;
  if (t.split(/\s+/).some((word) => word.startsWith(q))) return 0.8;
  if (t.includes(q)) return 0.6;
  return keywords.some((k) => k.toLowerCase().includes(q)) ? 0.2 : 0;
}
