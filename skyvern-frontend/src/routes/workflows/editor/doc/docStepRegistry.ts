import type { Node } from "@xyflow/react";
import { RunEngine } from "@/api/types";
import {
  debuggableWorkflowBlockTypes,
  type WorkflowBlockType,
} from "../../types/workflowTypes";
import { blockTypeFromNode } from "../nodes/blockTypeFromNode";
import { workflowBlockTitle } from "../nodes/types";
import { humanizeBlockLabel } from "../../copilot/blockLabel";
import { stripJinjaReferences } from "./docTextUtils";
import { nodeLibraryItems } from "../panels/nodeLibraryItems";

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

const OUTPUT_BLOCK_TYPES = new Set<WorkflowBlockType>([
  "agent_step",
  "extraction",
  "text_prompt",
  "file_url_parser",
  "pdf_parser",
  "web_search",
  "http_request",
  "code",
  "data_export",
  "powerbi_query",
  "human_interaction",
  "email_inbox",
  "google_sheets_read",
  "file_download",
  "download_to_s3",
  "workflow_trigger",
]);

/** Whether the step hands data to later steps; the others don't name an output. */
export function docStepProducesData(node: Node): boolean {
  const blockType = docBlockType(node);
  if (!blockType) return false;
  return OUTPUT_BLOCK_TYPES.has(blockType);
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
  // The Python itself stays in the visual editor; Doc edits the prompt that describes it.
  codeBlock: [field("prompt", "Prompt")],
  // Settings-driven steps: the main line is the field that drives the step, so what is typed is what runs.
  url: [field("url", "URL")],
  http_request: [field("url", "URL")],
  sendEmail: [field("body", "Body")],
  fileParser: [field("fileUrl", "File")],
  pdfParser: [field("fileUrl", "File")],
  download: [field("url", "URL")],
  upload: [field("path", "Folder")],
  fileUpload: [field("path", "Folder")],
  googleSheetsRead: [field("spreadsheetUrl", "Spreadsheet")],
  googleSheetsWrite: [field("spreadsheetUrl", "Spreadsheet")],
  web_search: [field("query", "Query"), field("prompt", "Prompt")],
  powerBIQuery: [field("query", "Query")],
  emailInbox: [field("prompt", "Match prompt")],
  terminate: [field("reason", "Reason")],
  pdfFill: [field("prompt", "Prompt")],
  splitPdf: [field("prompt", "Prompt")],
  agentStep: [field("prompt", "Prompt")],
  fileDownload: [field("navigationGoal", "Download goal")],
};

// As at run time, any nonempty loopVariableReference wins over loopValue, even one naming no list (`{{ }}`); a new loop uses it too.
function loopSourceField(
  data: Record<string, unknown>,
): "loopVariableReference" | "loopValue" {
  return !data.loopVariableReference &&
    typeof data.loopValue === "string" &&
    data.loopValue.trim()
    ? "loopValue"
    : "loopVariableReference";
}

export const loopSourceKey = (node: Node) => loopSourceField(node.data);

export function getDocProseFields(node: Node): DocProseField[] {
  // "Complete if" isn't saved for 2.0 navigation, so it isn't shown there.
  if (node.type === "navigation")
    return node.data.engine === RunEngine.SkyvernV2
      ? [field("prompt", "Prompt")]
      : [
          field("navigationGoal", "Goal"),
          field("completeCriterion", "Complete if"),
        ];
  if (node.type === "loop")
    return node.data.loopKind === "while"
      ? [field("whileConditionExpression", "Condition")]
      : [];
  return proseFields[node.type ?? ""] ?? [];
}

/** The prose fields with text, in order; the first is the line the Doc shows as the step's prompt. */
export function getDocFilledProseFields(
  node: Node,
  keep: readonly string[] = [],
): (DocProseField & { value: string })[] {
  return getDocProseFields(node).flatMap((f) => {
    const value =
      typeof node.data[f.key] === "string" ? (node.data[f.key] as string) : "";
    return value.trim() || keep.includes(f.key) ? [{ ...f, value }] : [];
  });
}

// Steps whose main line is a setting (a URL, a file), not an instruction; their title stays the summary.
const SETTING_MAIN = new Set([
  "url",
  "http_request",
  "fileParser",
  "pdfParser",
  "download",
  "upload",
  "fileUpload",
  "googleSheetsRead",
  "googleSheetsWrite",
  "web_search",
  "powerBIQuery",
  "terminate",
  "loop",
]);

export const docMainIsSetting = (node: Node) =>
  SETTING_MAIN.has(node.type ?? "");

/** True when the step has no field that drives it; its description is a note the agent does not read. */
export function docStepIsNoteOnly(node: Node): boolean {
  return (
    getDocProseFields(node).length === 0 &&
    node.type !== "conditional" &&
    node.type !== "loop"
  );
}

const NO_INSTRUCTIONS = "No instructions yet";
const REFERENCE_HINT = "Type @ to use an input or an earlier step's result.";
const editingHints: Record<string, string> = {
  validation: `Write what must be true for this check to pass. ${REFERENCE_HINT}`,
  human_interaction: `Write what the approver should look at before deciding. ${REFERENCE_HINT}`,
};

/** What an empty main line shows: the step's own summary when it has one, else an editing hint while editing. */
export function getDocMainPlaceholder(node: Node, editable: boolean): string {
  return (
    fixedStepSummary(node) ??
    (editable
      ? (editingHints[node.type ?? ""] ??
        `Write what the agent should do. ${REFERENCE_HINT}`)
      : NO_INSTRUCTIONS)
  );
}

// Settings a step cannot run without; Doc shows each as a chip while it is empty.
const requiredSettings: Record<string, Array<[string, string]>> = {
  url: [["url", "URL"]],
  http_request: [["url", "URL"]],
  download: [["url", "URL"]],
  fileParser: [["fileUrl", "File"]],
  pdfParser: [["fileUrl", "File"]],
  pdfFill: [["fileUrl", "File"]],
  splitPdf: [["fileUrl", "File"]],
  googleSheetsRead: [["spreadsheetUrl", "Spreadsheet"]],
  googleSheetsWrite: [["spreadsheetUrl", "Spreadsheet"]],
  web_search: [["query", "Query"]],
  powerBIQuery: [["query", "Query"]],
  workflowTrigger: [["workflowPermanentId", "Agent"]],
  wait: [["waitInSeconds", "Seconds"]],
  sendEmail: [["recipients", "Recipients"]],
};

export function getDocMissingSettings(node: Node): string[] {
  return (requiredSettings[node.type ?? ""] ?? []).flatMap(([key, label]) =>
    settingIsSet(node, key) ? [] : [label],
  );
}

// A Wait needs a whole number of seconds of at least 1; "" or "0" would save as a wait that does nothing or is refused.
function settingIsSet(node: Node, key: string): boolean {
  if (node.type === "wait")
    return /^\d+$/.test(text(node, key)) && Number(text(node, key)) >= 1;
  return Boolean(text(node, key));
}

const text = (node: Node, key: string) =>
  typeof node.data[key] === "string" ? (node.data[key] as string).trim() : "";

// The list a for-each loop goes through, or "" when its source names none.
export function loopList(data: Record<string, unknown>): string {
  const source = data[loopSourceField(data)];
  const list = typeof source === "string" ? source.trim() : "";
  return stripJinjaReferences(list).trim() ? list : "";
}

export const loopHasNoList = (data: Record<string, unknown>) =>
  data.loopKind !== "while" && loopList(data) === "";

export function getDocStepSummary(node: Node): string {
  return fixedStepSummary(node) ?? NO_INSTRUCTIONS;
}

// Null for a step driven by its prompt, which has no summary of its own.
function fixedStepSummary(node: Node): string | null {
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
    case "powerBIQuery":
      return "Run a Power BI query";
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
        : loopList(node.data)
          ? `For each of ${loopList(node.data)}`
          : "For each item";
    default:
      return null;
  }
}

// Steps whose summary line is a setting (a loop condition, a stop reason, a wait), not a fixed phrase.
const SUMMARY_IS_SETTING = new Set([
  "loop",
  "terminate",
  "wait",
  "printPage",
  "workflowTrigger",
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
  const prose = SETTING_MAIN.has(node.type ?? "")
    ? ""
    : getDocProseFields(node)
        .map((f) => text(node, f.key))
        .find(Boolean);
  const title =
    prose ||
    description.trim() ||
    (getDocProseFields(node).length && !SETTING_MAIN.has(node.type ?? "")
      ? String(node.data.label)
      : getDocStepSummary(node));
  return clip(title);
}

/** A step type's name from the full library, so a type the picker hides today (Agent, Power BI) still has its title. */
export const nodeLibraryTitle = (type: string) =>
  nodeLibraryItems.find((item) => item.nodeType === type)?.title ?? type;

/** The name stem for a step type: "Go to URL Block" gives go_to_url. */
export function labelStem(title: string): string {
  return (
    title
      .replace(/ Block$/, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .replace(/^(?=\d)/, "step_") || "step"
  );
}

/** The step's label in words, or null for a default name (block_3, extraction_1) that says no more than its type. */
export function docStepCaption(node: Node): string | null {
  const label = typeof node.data.label === "string" ? node.data.label : "";
  const blockType = docBlockType(node);
  const stems = [
    "block",
    labelStem(nodeLibraryTitle(node.type ?? "")),
    ...(blockType ? [labelStem(workflowBlockTitle[blockType])] : []),
  ];
  const unversioned = label.replace(/_v\d+$/, "");
  const generic = stems.some(
    (stem) =>
      unversioned.startsWith(stem) &&
      /^(_\d+)?$/.test(unversioned.slice(stem.length)),
  );
  if (!label.trim() || generic) return null;
  // Sentence case reads as a title; a word with capitals after its first letter (PDF) keeps them.
  return humanizeBlockLabel(label)
    .split(" ")
    .map((word, i) =>
      i > 0 && word.slice(1) === word.slice(1).toLowerCase()
        ? word.toLowerCase()
        : word,
    )
    .join(" ");
}

function clip(title: string): string {
  const plain = stripJinjaReferences(title);
  return plain.length > 60 ? `${plain.slice(0, 59)}…` : plain;
}

/** A loop line that already starts "For each …" is a whole sentence, so the fixed "For each of" lead-in is left off. */
export function loopLineLeadsIn(description: string | null): boolean {
  return /^\s*for each\b/i.test(description ?? "");
}

// Words the Doc shows for a step that its library title doesn't contain.
const STEP_ALIASES: Record<string, string[]> = {
  loop: ["for each", "for", "repeat"],
  conditional: ["if", "else", "branch"],
};

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
  if (
    STEP_ALIASES[t]?.some((alias) => alias.startsWith(q) || q.startsWith(alias))
  )
    return 0.85;
  return keywords.some((k) => k.toLowerCase().includes(q)) ? 0.2 : 0;
}
