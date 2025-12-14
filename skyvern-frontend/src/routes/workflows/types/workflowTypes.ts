import { JsonObjectExtendable } from "@/types";
import { ProxyLocation, RunEngine } from "@/api/types";

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
  bitwarden_collection_id: string | null;
  bitwarden_item_id: string | null;
  url_parameter_key: string | null;
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

export type BitwardenCreditCardDataParameter = WorkflowParameterBase & {
  parameter_type: "bitwarden_credit_card_data";
  workflow_id: string;
  bitwarden_credit_card_data_parameter_id: string;
  bitwarden_client_id_aws_secret_key: string;
  bitwarden_client_secret_aws_secret_key: string;
  bitwarden_master_password_aws_secret_key: string;
  bitwarden_collection_id: string;
  bitwarden_item_id: string;
  created_at: string;
  modified_at: string;
  deleted_at: string | null;
};

export type OnePasswordCredentialParameter = WorkflowParameterBase & {
  parameter_type: "onepassword";
  workflow_id: string;
  onepassword_credential_parameter_id: string;
  vault_id: string;
  item_id: string;
  created_at: string;
  modified_at: string;
  deleted_at: string | null;
};

export type AzureVaultCredentialParameter = WorkflowParameterBase & {
  parameter_type: "azure_vault_credential";
  workflow_id: string;
  azure_vault_credential_parameter_id: string;
  vault_name: string;
  username_key: string;
  password_key: string;
  totp_secret_key: string | null;
  created_at: string;
  modified_at: string;
  deleted_at: string | null;
};

export type CredentialParameter = WorkflowParameterBase & {
  parameter_type: "credential";
  workflow_id: string;
  credential_parameter_id: string;
  credential_id: string;
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
  CredentialId: "credential_id",
} as const;

export type WorkflowParameterValueType =
  (typeof WorkflowParameterValueType)[keyof typeof WorkflowParameterValueType];

export const WorkflowParameterTypes = {
  Workflow: "workflow",
  Context: "context",
  Output: "output",
  AWS_Secret: "aws_secret",
  Bitwarden_Login_Credential: "bitwarden_login_credential",
  Bitwarden_Sensitive_Information: "bitwarden_sensitive_information",
  Bitwarden_Credit_Card_Data: "bitwarden_credit_card_data",
  OnePassword: "onepassword",
  Azure_Vault_Credential: "azure_vault_credential",
  Credential: "credential",
} as const;

export type WorkflowParameterType =
  (typeof WorkflowParameterTypes)[keyof typeof WorkflowParameterTypes];

export function isDisplayedInWorkflowEditor(
  parameter: Parameter,
): parameter is
  | WorkflowParameter
  | ContextParameter
  | BitwardenCreditCardDataParameter
  | BitwardenLoginCredentialParameter
  | BitwardenSensitiveInformationParameter
  | OnePasswordCredentialParameter
  | AzureVaultCredentialParameter
  | CredentialParameter {
  return (
    parameter.parameter_type === WorkflowParameterTypes.Workflow ||
    parameter.parameter_type ===
      WorkflowParameterTypes.Bitwarden_Login_Credential ||
    parameter.parameter_type === WorkflowParameterTypes.Context ||
    parameter.parameter_type ===
      WorkflowParameterTypes.Bitwarden_Sensitive_Information ||
    parameter.parameter_type ===
      WorkflowParameterTypes.Bitwarden_Credit_Card_Data ||
    parameter.parameter_type === WorkflowParameterTypes.OnePassword ||
    parameter.parameter_type ===
      WorkflowParameterTypes.Azure_Vault_Credential ||
    parameter.parameter_type === WorkflowParameterTypes.Credential
  );
}

export type Parameter =
  | WorkflowParameter
  | OutputParameter
  | ContextParameter
  | BitwardenLoginCredentialParameter
  | BitwardenSensitiveInformationParameter
  | BitwardenCreditCardDataParameter
  | OnePasswordCredentialParameter
  | AzureVaultCredentialParameter
  | AWSSecretParameter
  | CredentialParameter;

export type WorkflowBlock =
  | TaskBlock
  | ForLoopBlock
  | ConditionalBlock
  | TextPromptBlock
  | CodeBlock
  | UploadToS3Block
  | FileUploadBlock
  | DownloadToS3Block
  | SendEmailBlock
  | FileURLParserBlock
  | ValidationBlock
  | HumanInteractionBlock
  | ActionBlock
  | NavigationBlock
  | ExtractionBlock
  | LoginBlock
  | WaitBlock
  | FileDownloadBlock
  | PDFParserBlock
  | Taskv2Block
  | URLBlock
  | HttpRequestBlock;

export const WorkflowBlockTypes = {
  Task: "task",
  ForLoop: "for_loop",
  Conditional: "conditional",
  Code: "code",
  TextPrompt: "text_prompt",
  DownloadToS3: "download_to_s3",
  UploadToS3: "upload_to_s3",
  FileUpload: "file_upload",
  SendEmail: "send_email",
  FileURLParser: "file_url_parser",
  Validation: "validation",
  HumanInteraction: "human_interaction",
  Action: "action",
  Navigation: "navigation",
  Extraction: "extraction",
  Login: "login",
  Wait: "wait",
  FileDownload: "file_download",
  PDFParser: "pdf_parser",
  Taskv2: "task_v2",
  URL: "goto_url",
  HttpRequest: "http_request",
} as const;

// all of them
export const debuggableWorkflowBlockTypes: Set<WorkflowBlockType> = new Set(
  Object.values(WorkflowBlockTypes),
);

export const scriptableWorkflowBlockTypes: Set<WorkflowBlockType> = new Set([
  "action",
  "extraction",
  "file_download",
  "goto_url",
  "login",
  "navigation",
  "task",
  "task_v2",
  "validation",
]);

export function isTaskVariantBlock(item: {
  block_type: WorkflowBlockType;
}): boolean {
  return (
    item.block_type === "task" ||
    item.block_type === "navigation" ||
    item.block_type === "action" ||
    item.block_type === "extraction" ||
    item.block_type === "validation" ||
    item.block_type === "login" ||
    item.block_type === "file_download"
  );
}

export type WorkflowBlockType =
  (typeof WorkflowBlockTypes)[keyof typeof WorkflowBlockTypes];

export const WorkflowEditorParameterTypes = {
  Workflow: "workflow",
  Credential: "credential",
  Secret: "secret",
  Context: "context",
  CreditCardData: "creditCardData",
  OnePassword: "onepassword",
} as const;

export type WorkflowEditorParameterType =
  (typeof WorkflowEditorParameterTypes)[keyof typeof WorkflowEditorParameterTypes];

export type WorkflowBlockBase = {
  label: string;
  block_type: WorkflowBlockType;
  output_parameter: OutputParameter;
  continue_on_failure: boolean;
  next_loop_on_failure?: boolean;
  model: WorkflowModel | null;
  next_block_label?: string | null;
};

export const BranchCriteriaTypes = {
  Jinja2Template: "jinja2_template",
} as const;

export type BranchCriteriaType =
  (typeof BranchCriteriaTypes)[keyof typeof BranchCriteriaTypes];

export type BranchCriteria = {
  criteria_type: BranchCriteriaType;
  expression: string;
  description: string | null;
};

export type BranchCondition = {
  id: string;
  criteria: BranchCriteria | null;
  next_block_label: string | null;
  description: string | null;
  is_default: boolean;
};

export type ConditionalBlock = WorkflowBlockBase & {
  block_type: "conditional";
  branch_conditions: Array<BranchCondition>;
};

export type TaskBlock = WorkflowBlockBase & {
  block_type: "task";
  url: string | null;
  title: string;
  navigation_goal: string | null;
  data_extraction_goal: string | null;
  data_schema: Record<string, unknown> | string | null;
  complete_criterion: string | null;
  terminate_criterion: string | null;
  error_code_mapping: Record<string, string> | null;
  max_retries?: number;
  max_steps_per_run?: number | null;
  parameters: Array<WorkflowParameter>;
  complete_on_download?: boolean;
  download_suffix?: string | null;
  totp_verification_url?: string | null;
  totp_identifier?: string | null;
  disable_cache?: boolean;
  include_action_history_in_verification: boolean;
  engine: RunEngine | null;
};

export type Taskv2Block = WorkflowBlockBase & {
  block_type: "task_v2";
  prompt: string;
  url: string | null;
  totp_verification_url: string | null;
  totp_identifier: string | null;
  max_steps: number | null;
  disable_cache: boolean;
};

export type ForLoopBlock = WorkflowBlockBase & {
  block_type: "for_loop";
  loop_over: WorkflowParameter;
  loop_blocks: Array<WorkflowBlock>;
  loop_variable_reference: string | null;
  complete_if_empty: boolean;
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

export type FileUploadBlock = WorkflowBlockBase & {
  block_type: "file_upload";
  path: string;
  storage_type: "s3" | "azure";
  s3_bucket: string | null;
  region_name: string | null;
  aws_access_key_id: string | null;
  aws_secret_access_key: string | null;
  azure_storage_account_name: string | null;
  azure_storage_account_key: string | null;
  azure_blob_container_name: string | null;
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
  file_type: "csv" | "excel" | "pdf";
  json_schema: Record<string, unknown> | null;
};

export type ValidationBlock = WorkflowBlockBase & {
  block_type: "validation";
  complete_criterion: string | null;
  terminate_criterion: string | null;
  error_code_mapping: Record<string, string> | null;
  parameters: Array<WorkflowParameter>;
  disable_cache?: boolean;
};

export type HumanInteractionBlock = WorkflowBlockBase & {
  block_type: "human_interaction";

  instructions: string;
  positive_descriptor: string;
  negative_descriptor: string;
  timeout_seconds: number;

  sender: string;
  recipients: Array<string>;
  subject: string;
  body: string;
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
  disable_cache?: boolean;
  engine: RunEngine | null;
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
  disable_cache?: boolean;
  complete_criterion: string | null;
  terminate_criterion: string | null;
  engine: RunEngine | null;
  include_action_history_in_verification: boolean;
};

export type ExtractionBlock = WorkflowBlockBase & {
  block_type: "extraction";
  data_extraction_goal: string | null;
  url: string | null;
  title: string;
  data_schema: Record<string, unknown> | string | null;
  max_retries?: number;
  max_steps_per_run?: number | null;
  parameters: Array<WorkflowParameter>;
  disable_cache?: boolean;
  engine: RunEngine | null;
};

export type LoginBlock = WorkflowBlockBase & {
  block_type: "login";
  url: string | null;
  title: string;
  navigation_goal: string | null;
  error_code_mapping: Record<string, string> | null;
  max_retries?: number;
  max_steps_per_run?: number | null;
  parameters: Array<WorkflowParameter>;
  totp_verification_url?: string | null;
  totp_identifier?: string | null;
  disable_cache?: boolean;
  complete_criterion: string | null;
  terminate_criterion: string | null;
  engine: RunEngine | null;
};

export type WaitBlock = WorkflowBlockBase & {
  block_type: "wait";
  wait_sec?: number;
};

export type FileDownloadBlock = WorkflowBlockBase & {
  block_type: "file_download";
  url: string | null;
  title: string;
  navigation_goal: string | null;
  error_code_mapping: Record<string, string> | null;
  max_retries?: number;
  max_steps_per_run?: number | null;
  download_suffix?: string | null;
  parameters: Array<WorkflowParameter>;
  totp_verification_url?: string | null;
  totp_identifier?: string | null;
  disable_cache?: boolean;
  engine: RunEngine | null;
  download_timeout: number | null; // seconds
};

export type PDFParserBlock = WorkflowBlockBase & {
  block_type: "pdf_parser";
  file_url: string;
  json_schema: Record<string, unknown> | null;
};

export type URLBlock = WorkflowBlockBase & {
  block_type: "goto_url";
  url: string;
};

export type HttpRequestBlock = WorkflowBlockBase & {
  block_type: "http_request";
  method: string;
  url: string | null;
  headers: Record<string, string> | null;
  body: Record<string, unknown> | null;
  files: Record<string, string> | null; // Dictionary mapping field names to file paths/URLs
  timeout: number;
  follow_redirects: boolean;
  parameters: Array<WorkflowParameter>;
};

export type WorkflowDefinition = {
  version?: number | null;
  parameters: Array<Parameter>;
  blocks: Array<WorkflowBlock>;
};

export type WorkflowApiResponse = {
  workflow_id: string;
  organization_id: string;
  is_saved_task: boolean;
  is_template: boolean;
  title: string;
  workflow_permanent_id: string;
  version: number;
  description: string;
  workflow_definition: WorkflowDefinition;
  proxy_location: ProxyLocation | null;
  webhook_callback_url: string | null;
  extra_http_headers: Record<string, string> | null;
  persist_browser_session: boolean;
  model: WorkflowModel | null;
  totp_verification_url: string | null;
  totp_identifier: string | null;
  max_screenshot_scrolls: number | null;
  status: string | null;
  created_at: string;
  modified_at: string;
  deleted_at: string | null;
  run_with: string | null; // 'agent' or 'code'
  cache_key: string | null;
  ai_fallback: boolean | null;
  run_sequentially: boolean | null;
  sequential_key: string | null;
  folder_id: string | null;
  import_error: string | null;
};

export type WorkflowSettings = {
  proxyLocation: ProxyLocation | null;
  webhookCallbackUrl: string | null;
  persistBrowserSession: boolean;
  model: WorkflowModel | null;
  maxScreenshotScrolls: number | null;
  extraHttpHeaders: string | null;
  runWith: string | null; // 'agent' or 'code'
  scriptCacheKey: string | null;
  aiFallback: boolean | null;
  runSequentially: boolean;
  sequentialKey: string | null;
};

export type WorkflowModel = JsonObjectExtendable<{ model_name: string }>;

export function isOutputParameter(
  parameter: Parameter,
): parameter is OutputParameter {
  return parameter.parameter_type === "output";
}

export type ImprovePromptForWorkflowResponse = {
  error: string | null;
  improved: string;
  original: string;
};
