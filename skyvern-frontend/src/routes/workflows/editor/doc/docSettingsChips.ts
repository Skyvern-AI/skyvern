import type { Node } from "@xyflow/react";
import { RunEngine } from "@/api/types";
import { taskNodeDefaultData } from "../nodes/TaskNode/types";
import { taskv2NodeDefaultData } from "../nodes/Taskv2Node/types";
import { navigationNodeDefaultData } from "../nodes/NavigationNode/types";
import { actionNodeDefaultData } from "../nodes/ActionNode/types";
import { extractionNodeDefaultData } from "../nodes/ExtractionNode/types";
import { loginNodeDefaultData } from "../nodes/LoginNode/types";
import { validationNodeDefaultData } from "../nodes/ValidationNode/types";
import { humanInteractionNodeDefaultData } from "../nodes/HumanInteractionNode/types";
import { waitNodeDefaultData } from "../nodes/WaitNode/types";
import { terminateNodeDefaultData } from "../nodes/TerminateNode/types";
import { loopNodeDefaultData } from "../nodes/LoopNode/types";
import { conditionalNodeDefaultData } from "../nodes/ConditionalNode/types";
import { codeBlockNodeDefaultData } from "../nodes/CodeBlockNode/types";
import { textPromptNodeDefaultData } from "../nodes/TextPromptNode/types";
import { sendEmailNodeDefaultData } from "../nodes/SendEmailNode/types";
import { emailInboxNodeDefaultData } from "../nodes/EmailInboxNode/types";
import { fileDownloadNodeDefaultData } from "../nodes/FileDownloadNode/types";
import { fileUploadNodeDefaultData } from "../nodes/FileUploadNode/types";
import { downloadNodeDefaultData } from "../nodes/DownloadNode/types";
import { uploadNodeDefaultData } from "../nodes/UploadNode/types";
import { fileParserNodeDefaultData } from "../nodes/FileParserNode/types";
import { pdfParserNodeDefaultData } from "../nodes/PDFParserNode/types";
import { pdfFillNodeDefaultData } from "../nodes/PdfFillNode/types";
import { splitPdfNodeDefaultData } from "../nodes/SplitPdfNode/types";
import { dataExportNodeDefaultData } from "../nodes/DataExportNode/types";
import { urlNodeDefaultData } from "../nodes/URLNode/types";
import { webSearchNodeDefaultData } from "../nodes/WebSearchNode/types";
import { httpRequestNodeDefaultData } from "../nodes/HttpRequestNode/types";
import { printPageNodeDefaultData } from "../nodes/PrintPageNode/types";
import { workflowTriggerNodeDefaultData } from "../nodes/WorkflowTriggerNode/types";
import { googleSheetsReadNodeDefaultData } from "../nodes/GoogleSheetsReadNode/types";
import { googleSheetsWriteNodeDefaultData } from "../nodes/GoogleSheetsWriteNode/types";

const defaults: Record<string, Record<string, unknown>> = {
  task: taskNodeDefaultData,
  taskv2: taskv2NodeDefaultData,
  navigation: navigationNodeDefaultData,
  action: actionNodeDefaultData,
  extraction: extractionNodeDefaultData,
  login: loginNodeDefaultData,
  validation: validationNodeDefaultData,
  human_interaction: humanInteractionNodeDefaultData,
  wait: waitNodeDefaultData,
  terminate: terminateNodeDefaultData,
  loop: loopNodeDefaultData,
  conditional: conditionalNodeDefaultData,
  codeBlock: codeBlockNodeDefaultData,
  textPrompt: textPromptNodeDefaultData,
  sendEmail: sendEmailNodeDefaultData,
  emailInbox: emailInboxNodeDefaultData,
  fileDownload: fileDownloadNodeDefaultData,
  fileUpload: fileUploadNodeDefaultData,
  download: downloadNodeDefaultData,
  upload: uploadNodeDefaultData,
  fileParser: fileParserNodeDefaultData,
  pdfParser: pdfParserNodeDefaultData,
  pdfFill: pdfFillNodeDefaultData,
  splitPdf: splitPdfNodeDefaultData,
  dataExport: dataExportNodeDefaultData,
  url: urlNodeDefaultData,
  web_search: webSearchNodeDefaultData,
  http_request: httpRequestNodeDefaultData,
  printPage: printPageNodeDefaultData,
  workflowTrigger: workflowTriggerNodeDefaultData,
  googleSheetsRead: googleSheetsReadNodeDefaultData,
  googleSheetsWrite: googleSheetsWriteNodeDefaultData,
};

// API defaults can differ from the empty values used by new-node forms.
const serverDefaults: Record<string, unknown> = {
  maxRetries: 0,
  continueOnFailure: false,
  nextLoopOnFailure: false,
  disableCache: false,
  allowDownloads: false,
  completeOnDownload: false,
};

const chipFields = {
  engine: "Engine",
  maxSteps: "Max steps",
  maxStepsOverride: "Max steps",
  maxRetries: "Max retries",
  continueOnFailure: "Continue on failure",
  nextLoopOnFailure: "Next loop on failure",
  model: "Model",
  disableCache: "Cache disabled",
  allowDownloads: "Complete on download",
  completeOnDownload: "Complete on download",
};

const BROWSER = [
  "engine",
  "continueOnFailure",
  "nextLoopOnFailure",
  "model",
  "disableCache",
];
const ON_FAILURE = ["continueOnFailure", "nextLoopOnFailure"];
// The settings above that each step type's own form edits; a type not listed edits none.
const formSettings: Record<string, string[]> = {
  task: [...BROWSER, "maxStepsOverride", "allowDownloads"],
  navigation: [...BROWSER, "maxSteps", "maxStepsOverride", "allowDownloads"],
  action: [...BROWSER, "allowDownloads"],
  extraction: [...BROWSER, "maxStepsOverride"],
  login: [...BROWSER, "maxStepsOverride"],
  fileDownload: [...BROWSER, "maxStepsOverride"],
  validation: BROWSER,
  taskv2: ["maxSteps", "model", "disableCache"],
  fileParser: [...ON_FAILURE, "model"],
  loop: ON_FAILURE,
  conditional: ON_FAILURE,
  workflowTrigger: ON_FAILURE,
  http_request: ["continueOnFailure"],
  web_search: ["continueOnFailure", "model"],
  textPrompt: ["model"],
  pdfParser: ["model"],
  pdfFill: ["model"],
  splitPdf: ["model"],
};

export const customSettingsText = (count: number) =>
  `${count} custom setting${count === 1 ? "" : "s"}`;

export const customSettingsShortText = (count: number) => `${count} custom`;

const engines: Record<string, string> = {
  "skyvern-1.0": "Skyvern 1.0",
  "skyvern-2.0": "Skyvern 2.0",
  "skyvern-3.0": "Skyvern 3.0",
  "openai-cua": "OpenAI CUA",
  "anthropic-cua": "Anthropic CUA",
};

function formatSetting(key: string, value: unknown): string | null {
  if (key === "model")
    return typeof value === "object" &&
      value !== null &&
      "model_name" in value &&
      typeof value.model_name === "string" &&
      value.model_name
      ? value.model_name
      : null;
  if (key === "engine")
    return typeof value === "string" ? (engines[value] ?? null) : null;
  if (typeof value === "boolean") return value ? "On" : "Off";
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

export type DocSettingRow = {
  key: string;
  label: string;
  value: string;
  custom: boolean;
};

/** The common settings the step's own form edits, with their values; `custom` when one differs from the default. */
export const getDocSettingsRows = (node: Node) =>
  settingRows(node).filter((row) =>
    formSettings[node.type ?? ""]?.includes(row.key),
  );

function settingRows(node: Node): DocSettingRow[] {
  const baseline = defaults[node.type ?? ""];
  if (!baseline) return [];
  // Navigation keeps max steps in both keys; only the one its engine saves counts.
  const unsaved =
    node.type !== "navigation"
      ? null
      : node.data.engine === RunEngine.SkyvernV2
        ? "maxStepsOverride"
        : "maxSteps";
  return Object.entries(chipFields).flatMap(([key, label]) => {
    if (key === unsaved) return [];
    const value = node.data[key];
    const defaultValue =
      baseline[key] ?? (key === "nextLoopOnFailure" ? false : undefined);
    const formatted = formatSetting(key, value);
    // A model without a name (`{ model_name: "" }`) is not an override.
    const empty =
      value == null || value === "" || (key === "model" && !formatted);
    if (empty && !(key in baseline)) return [];
    // A value set but not in a known form shows as written, never as the default.
    const raw = !empty && formatted === null;
    const custom =
      raw ||
      (!empty &&
        value !== defaultValue &&
        value !== serverDefaults[key] &&
        // A model object equal by name to the default is the default.
        (key !== "model" || formatSetting(key, defaultValue) !== formatted));
    const shown = raw
      ? typeof value === "object"
        ? JSON.stringify(value)
        : String(value)
      : (formatted ??
        formatSetting(key, defaultValue ?? serverDefaults[key]) ??
        "Default");
    return [{ key, label, value: shown, custom }];
  });
}

export function getDocSettingsChips(
  node: Node,
): { key: string; label: string }[] {
  return settingRows(node)
    .filter(
      (row) =>
        row.custom && formatSetting(row.key, node.data[row.key]) !== null,
    )
    .map((row) => ({ key: row.key, label: `${row.label}: ${row.value}` }));
}
