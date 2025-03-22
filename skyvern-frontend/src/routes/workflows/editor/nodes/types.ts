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
  upload_to_s3: "Upload To S3",
  file_upload: "Upload Files",
  validation: "Validation",
  wait: "Wait",
  pdf_parser: "PDF Parser",
  task_v2: "Task v2",
  goto_url: "Go to URL",
};
