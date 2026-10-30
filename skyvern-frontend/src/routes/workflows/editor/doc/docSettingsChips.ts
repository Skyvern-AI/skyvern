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

export function getDocSettingsChips(
  node: Node,
): { key: string; label: string }[] {
  const baseline = defaults[node.type ?? ""];
  if (!baseline) return [];
  const chips = Object.entries(chipFields).flatMap(([key, label]) => {
    const value = node.data[key];
    const defaultValue =
      baseline[key] ?? (key === "nextLoopOnFailure" ? false : undefined);
    if (
      value == null ||
      value === "" ||
      value === defaultValue ||
      value === serverDefaults[key]
    )
      return [];
    let formatted: string;
    if (key === "model") {
      if (
        typeof value !== "object" ||
        !("model_name" in value) ||
        typeof value.model_name !== "string"
      )
        return [];
      formatted = value.model_name;
      if (!formatted) return [];
      if (
        typeof defaultValue === "object" &&
        defaultValue !== null &&
        "model_name" in defaultValue &&
        defaultValue.model_name === formatted
      )
        return [];
    } else if (key === "engine") {
      const engines: Record<string, string> = {
        "skyvern-1.0": "Skyvern 1.0",
        "skyvern-2.0": "Skyvern 2.0",
        "skyvern-3.0": "Skyvern 3.0",
        "openai-cua": "OpenAI CUA",
        "anthropic-cua": "Anthropic CUA",
        "yutori-navigator": "Yutori Navigator",
      };
      if (typeof value !== "string" || !engines[value]) return [];
      formatted = engines[value]!;
    } else if (typeof value === "boolean") formatted = value ? "On" : "Off";
    else if (typeof value === "number" && Number.isFinite(value))
      formatted = String(value);
    else return [];
    return [{ key, label: `${label}: ${formatted}` }];
  });
  if (node.type !== "navigation") return chips;
  // Navigation keeps max steps in both keys; only the one its engine saves counts.
  const unsaved =
    node.data.engine === RunEngine.SkyvernV2 ? "maxStepsOverride" : "maxSteps";
  return chips.filter((chip) => chip.key !== unsaved);
}
