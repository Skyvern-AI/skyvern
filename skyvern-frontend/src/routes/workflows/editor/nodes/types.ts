import { WorkflowBlockType } from "../../types/workflowTypes";
import type { WorkflowModel } from "../../types/workflowTypes";

export type NodeBaseData = {
  debuggable: boolean;
  label: string;
  continueOnFailure: boolean;
  nextLoopOnFailure?: boolean;
  editable: boolean;
  model: WorkflowModel | null;
  showCode?: boolean;
  comparisonColor?: string;
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
    sample: { type: "string" },
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

export const workflowBlockTitle: {
  [blockType in WorkflowBlockType]: string;
} = {
  action: "Browser Action",
  code: "Code",
  conditional: "Conditional",
  download_to_s3: "Download",
  extraction: "Extraction",
  file_download: "File Download",
  file_url_parser: "File Parser",
  for_loop: "Loop",
  login: "Login",
  navigation: "Browser Task",
  send_email: "Send Email",
  task: "Browser Task",
  text_prompt: "Text Prompt",
  upload_to_s3: "Upload To S3",
  file_upload: "Cloud Storage",
  validation: "Validation",
  human_interaction: "Human Interaction",
  wait: "Wait",
  pdf_parser: "PDF Parser",
  task_v2: "Browser Task v2",
  goto_url: "Go to URL",
  http_request: "HTTP Request",
};
