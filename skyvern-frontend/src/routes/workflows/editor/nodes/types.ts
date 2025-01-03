import { WorkflowBlockType } from "../../types/workflowTypes";

export type NodeBaseData = {
  label: string;
  continueOnFailure: boolean;
  editable: boolean;
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

export const workflowBlockTitle: {
  [blockType in WorkflowBlockType]: string;
} = {
  action: "Action",
  code: "Code",
  download_to_s3: "Download",
  extraction: "Extraction",
  file_download: "File Download",
  file_url_parser: "File Parser",
  for_loop: "Loop",
  login: "Login",
  navigation: "Navigation",
  send_email: "Send Email",
  task: "Task",
  text_prompt: "Text Prompt",
  upload_to_s3: "Upload",
  validation: "Validation",
  wait: "Wait",
};
