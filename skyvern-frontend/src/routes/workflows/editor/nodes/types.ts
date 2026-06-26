import {
  WorkflowBlockType,
  type WorkflowModel,
} from "../../types/workflowTypes";
import type { CodeBlockTitleSource } from "../../types/scriptTypes";

export type NodeBaseData = {
  debuggable: boolean;
  label: string;
  continueOnFailure: boolean;
  nextLoopOnFailure?: boolean;
  editable: boolean;
  model: WorkflowModel | null;
  showCode?: boolean;
  comparisonColor?: string;
  ignoreWorkflowSystemPrompt?: boolean;
  /**
   * Optional metadata used for conditional branches.
   * These values are only set on nodes that live within a conditional block.
   */
  conditionalBranchId?: string | null;
  conditionalLabel?: string | null;
  conditionalNodeId?: string | null;
  conditionalMergeLabel?: string | null;
};

export const errorMappingExampleValue = {
  sample_invalid_credentials: "if the credentials are incorrect, terminate",
} as const;

export const dataSchemaExampleValue = {
  type: "object",
  properties: {
    sample_field: { type: "string" },
  },
} as const;

export const dataSchemaExampleForFileExtraction = {
  type: "object",
  properties: {
    output: {
      type: "object",
      description: "All of the information extracted from the file",
    },
  },
};

export const CODE_BLOCK_FALLBACK_TITLE = "Code";
export const CODE_BLOCK_TITLE_MAX_LENGTH = 80;

function normalizeTitle(value: string | null | undefined): string | null {
  const normalized = value?.replace(/\s+/g, " ").trim();
  return normalized ? normalized : null;
}

function truncateTitle(value: string): string {
  if (value.length <= CODE_BLOCK_TITLE_MAX_LENGTH) {
    return value;
  }
  return `${value.slice(0, CODE_BLOCK_TITLE_MAX_LENGTH - 1).trimEnd()}…`;
}

export function getCodeBlockTitle(source: CodeBlockTitleSource): string {
  return truncateTitle(
    normalizeTitle(source.prompt) ??
      normalizeTitle(source.steps?.[0]?.title) ??
      CODE_BLOCK_FALLBACK_TITLE,
  );
}

export const workflowBlockTitle: {
  [blockType in WorkflowBlockType]: string;
} = {
  action: "Browser Action",
  code: CODE_BLOCK_FALLBACK_TITLE,
  conditional: "Conditional",
  download_to_s3: "Download",
  extraction: "Extraction",
  file_download: "File Download",
  file_url_parser: "File Parser",
  for_loop: "For Loop",
  while_loop: "While Loop",
  login: "Login",
  navigation: "Browser Task",
  send_email: "Send Email",
  task: "Browser Task",
  text_prompt: "Text Prompt",
  upload_to_s3: "Upload To S3",
  file_upload: "Cloud Storage",
  validation: "AI Validation",
  human_interaction: "Human Interaction",
  wait: "Wait",
  pdf_parser: "PDF Parser",
  task_v2: "Browser Task v2",
  goto_url: "Go to URL",
  http_request: "HTTP Request",
  print_page: "Print Page",
  workflow_trigger: "Agent Trigger",
  google_sheets_read: "Google Sheets Read",
  google_sheets_write: "Google Sheets Write",
  pdf_fill: "PDF Fill",
};
