export type WorkflowParameterBase = {
  parameter_type: WorkflowParameterType;
  key: string;
  description: string | null;
};

export type AWSSecretParameter = WorkflowParameterBase & {
  parameter_type: "aws_secret";
  workflow_id: string;
  aws_secret_parameter_id: string;
  aws_key: string;
  created_at: string;
  modified_at: string;
  deleted_at: string | null;
};

export type BitwardenLoginCredentialParameter = WorkflowParameterBase & {
  parameter_type: "bitwarden_login_credential";
  workflow_id: string;
  bitwarden_login_credential_parameter_id: string;
  bitwarden_client_id_aws_secret_key: string;
  bitwarden_client_secret_aws_secret_key: string;
  bitwarden_master_password_aws_secret_key: string;
  bitwarden_collection_id: string;
  url_parameter_key: string;
  created_at: string;
  modified_at: string;
  deleted_at: string | null;
};

export type BitwardenSensitiveInformationParameter = WorkflowParameterBase & {
  parameter_type: "bitwarden_sensitive_information";
  workflow_id: string;
  bitwarden_sensitive_information_parameter_id: string;
  bitwarden_client_id_aws_secret_key: string;
  bitwarden_client_secret_aws_secret_key: string;
  bitwarden_master_password_aws_secret_key: string;
  bitwarden_collection_id: string;
  bitwarden_identity_key: string;
  bitwarden_identity_fields: Array<string>;
  created_at: string;
  modified_at: string;
  deleted_at: string | null;
};

export type WorkflowParameter = WorkflowParameterBase & {
  parameter_type: "workflow";
  workflow_id: string;
  workflow_parameter_id: string;
  workflow_parameter_type: WorkflowParameterValueType;
  default_value: unknown;
  created_at: string;
  modified_at: string;
  deleted_at: string | null;
};

export type ContextParameter = WorkflowParameterBase & {
  parameter_type: "context";
  source: OutputParameter | ContextParameter | WorkflowParameter;
  value: unknown;
};

export type OutputParameter = WorkflowParameterBase & {
  parameter_type: "output";
  output_parameter_id: string;
  workflow_id: string;
  created_at: string;
  modified_at: string;
  deleted_at: string | null;
};

export const WorkflowParameterValueType = {
  String: "string",
  Integer: "integer",
  Float: "float",
  Boolean: "boolean",
  JSON: "json",
  FileURL: "file_url",
} as const;

export type WorkflowParameterValueType =
  (typeof WorkflowParameterValueType)[keyof typeof WorkflowParameterValueType];

export const WorkflowParameterType = {
  Workflow: "workflow",
  Context: "context",
  Output: "output",
  AWS_Secret: "aws_secret",
  Bitwarden_Login_Credential: "bitwarden_login_credential",
  Bitwarden_Sensitive_Information: "bitwarden_sensitive_information",
} as const;

export type WorkflowParameterType =
  (typeof WorkflowParameterType)[keyof typeof WorkflowParameterType];

export type Parameter =
  | WorkflowParameter
  | OutputParameter
  | ContextParameter
  | BitwardenLoginCredentialParameter
  | BitwardenSensitiveInformationParameter
  | AWSSecretParameter;

export type WorkflowBlock =
  | TaskBlock
  | ForLoopBlock
  | TextPromptBlock
  | CodeBlock
  | UploadToS3Block
  | DownloadToS3Block
  | SendEmailBlock
  | FileURLParserBlock
  | ValidationBlock
  | ActionBlock
  | NavigationBlock
  | ExtractionBlock;

export const WorkflowBlockType = {
  Task: "task",
  ForLoop: "for_loop",
  Code: "code",
  TextPrompt: "text_prompt",
  DownloadToS3: "download_to_s3",
  UploadToS3: "upload_to_s3",
  SendEmail: "send_email",
  FileURLParser: "file_url_parser",
  Validation: "validation",
  Action: "action",
  Navigation: "navigation",
  Extraction: "extraction",
} as const;

export type WorkflowBlockType =
  (typeof WorkflowBlockType)[keyof typeof WorkflowBlockType];

export type WorkflowBlockBase = {
  label: string;
  block_type: WorkflowBlockType;
  output_parameter: OutputParameter;
  continue_on_failure: boolean;
};

export type TaskBlock = WorkflowBlockBase & {
  block_type: "task";
  url: string | null;
  title: string;
  navigation_goal: string | null;
  data_extraction_goal: string | null;
  data_schema: Record<string, unknown> | null;
  error_code_mapping: Record<string, string> | null;
  max_retries?: number;
  max_steps_per_run?: number | null;
  parameters: Array<WorkflowParameter>;
  complete_on_download?: boolean;
  download_suffix?: string | null;
  totp_verification_url?: string | null;
  totp_identifier?: string | null;
  cache_actions: boolean;
};

export type ForLoopBlock = WorkflowBlockBase & {
  block_type: "for_loop";
  loop_over: WorkflowParameter;
  loop_blocks: Array<WorkflowBlock>;
};

export type CodeBlock = WorkflowBlockBase & {
  block_type: "code";
  code: string;
  parameters: Array<WorkflowParameter>;
};

export type TextPromptBlock = WorkflowBlockBase & {
  block_type: "text_prompt";
  llm_key: string;
  prompt: string;
  parameters: Array<WorkflowParameter>;
  json_schema: Record<string, unknown> | null;
};

export type DownloadToS3Block = WorkflowBlockBase & {
  block_type: "download_to_s3";
  url: string;
};

export type UploadToS3Block = WorkflowBlockBase & {
  block_type: "upload_to_s3";
  path: string;
};

export type SendEmailBlock = WorkflowBlockBase & {
  block_type: "send_email";
  smtp_host?: AWSSecretParameter;
  smtp_port?: AWSSecretParameter;
  smtp_username?: AWSSecretParameter;
  smtp_password?: AWSSecretParameter;
  sender: string;
  recipients: Array<string>;
  subject: string;
  body: string;
  file_attachments: Array<string>;
};

export type FileURLParserBlock = WorkflowBlockBase & {
  block_type: "file_url_parser";
  file_url: string;
  file_type: "csv";
};

export type ValidationBlock = WorkflowBlockBase & {
  block_type: "validation";
  complete_criterion: string | null;
  terminate_criterion: string | null;
  error_code_mapping: Record<string, string> | null;
  parameters: Array<WorkflowParameter>;
};

export type ActionBlock = WorkflowBlockBase & {
  block_type: "action";
  url: string | null;
  title: string;
  navigation_goal: string | null;
  error_code_mapping: Record<string, string> | null;
  max_retries?: number;
  max_steps_per_run?: number | null;
  parameters: Array<WorkflowParameter>;
  complete_on_download?: boolean;
  download_suffix?: string | null;
  totp_verification_url?: string | null;
  totp_identifier?: string | null;
  cache_actions: boolean;
};

export type NavigationBlock = WorkflowBlockBase & {
  block_type: "navigation";
  url: string | null;
  title: string;
  navigation_goal: string | null;
  error_code_mapping: Record<string, string> | null;
  max_retries?: number;
  max_steps_per_run?: number | null;
  parameters: Array<WorkflowParameter>;
  complete_on_download?: boolean;
  download_suffix?: string | null;
  totp_verification_url?: string | null;
  totp_identifier?: string | null;
  cache_actions: boolean;
};

export type ExtractionBlock = WorkflowBlockBase & {
  block_type: "extraction";
  data_extraction_goal: string | null;
  url: string | null;
  title: string;
  data_schema: Record<string, unknown> | null;
  max_retries?: number;
  max_steps_per_run?: number | null;
  parameters: Array<WorkflowParameter>;
  cache_actions: boolean;
};

export type WorkflowDefinition = {
  parameters: Array<Parameter>;
  blocks: Array<WorkflowBlock>;
};

export type WorkflowApiResponse = {
  workflow_id: string;
  organization_id: string;
  is_saved_task: boolean;
  title: string;
  workflow_permanent_id: string;
  version: number;
  description: string;
  workflow_definition: WorkflowDefinition;
  proxy_location: string;
  webhook_callback_url: string;
  totp_verification_url: string;
  created_at: string;
  modified_at: string;
  deleted_at: string | null;
};

export function isOutputParameter(
  parameter: Parameter,
): parameter is OutputParameter {
  return parameter.parameter_type === "output";
}
