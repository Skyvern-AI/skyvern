import { ProxyLocation, RunEngine } from "@/api/types";
import { WorkflowBlockType } from "./workflowTypes";
import { WorkflowModel } from "./workflowTypes";

export type WorkflowCreateYAMLRequest = {
  title: string;
  description?: string | null;
  proxy_location?: ProxyLocation | null;
  webhook_callback_url?: string | null;
  persist_browser_session?: boolean;
  model?: WorkflowModel | null;
  totp_verification_url?: string | null;
  workflow_definition: WorkflowDefinitionYAML;
  is_saved_task?: boolean;
  max_screenshot_scrolls?: number | null;
  extra_http_headers?: Record<string, string> | null;
  status?: string | null;
  run_with?: string | null;
  cache_key?: string | null;
  ai_fallback?: boolean;
  run_sequentially?: boolean;
  sequential_key?: string | null;
  folder_id?: string | null;
};

export type WorkflowDefinitionYAML = {
  version?: number | null;
  parameters: Array<ParameterYAML>;
  blocks: Array<BlockYAML>;
};

export type ParameterYAML =
  | WorkflowParameterYAML
  | BitwardenLoginCredentialParameterYAML
  | AWSSecretParameterYAML
  | BitwardenSensitiveInformationParameterYAML
  | BitwardenCreditCardDataParameterYAML
  | OnePasswordCredentialParameterYAML
  | AzureVaultCredentialParameterYAML
  | ContextParameterYAML
  | OutputParameterYAML
  | CredentialParameterYAML;

export type ParameterYAMLBase = {
  parameter_type: string;
  key: string;
  description?: string | null;
};

export type WorkflowParameterYAML = ParameterYAMLBase & {
  parameter_type: "workflow";
  workflow_parameter_type: string;
  default_value?: unknown;
};

export type BitwardenLoginCredentialParameterYAML = ParameterYAMLBase & {
  parameter_type: "bitwarden_login_credential";
  bitwarden_collection_id: string | null;
  bitwarden_item_id: string | null;
  url_parameter_key: string | null;
  bitwarden_client_id_aws_secret_key: string;
  bitwarden_client_secret_aws_secret_key: string;
  bitwarden_master_password_aws_secret_key: string;
};

export type AWSSecretParameterYAML = ParameterYAMLBase & {
  parameter_type: "aws_secret";
  aws_key: string;
};

export type BitwardenSensitiveInformationParameterYAML = ParameterYAMLBase & {
  parameter_type: "bitwarden_sensitive_information";
  bitwarden_collection_id: string;
  bitwarden_identity_key: string;
  bitwarden_identity_fields: Array<string>;
  bitwarden_client_id_aws_secret_key: string;
  bitwarden_client_secret_aws_secret_key: string;
  bitwarden_master_password_aws_secret_key: string;
};

export type BitwardenCreditCardDataParameterYAML = ParameterYAMLBase & {
  parameter_type: "bitwarden_credit_card_data";

  // bitwarden ids for the credit card item
  bitwarden_collection_id: string;
  bitwarden_item_id: string;

  bitwarden_client_id_aws_secret_key: string;
  bitwarden_client_secret_aws_secret_key: string;
  bitwarden_master_password_aws_secret_key: string;
};

export type OnePasswordCredentialParameterYAML = ParameterYAMLBase & {
  parameter_type: "onepassword";
  vault_id: string;
  item_id: string;
};

export type AzureVaultCredentialParameterYAML = ParameterYAMLBase & {
  parameter_type: "azure_vault_credential";
  vault_name: string;
  username_key: string;
  password_key: string;
  totp_secret_key: string | null;
};

export type ContextParameterYAML = ParameterYAMLBase & {
  parameter_type: "context";
  source_parameter_key: string;
};

export type OutputParameterYAML = ParameterYAMLBase & {
  parameter_type: "output";
};

export type CredentialParameterYAML = ParameterYAMLBase & {
  parameter_type: "credential";
  credential_id: string;
};

export type BlockYAML =
  | TaskBlockYAML
  | CodeBlockYAML
  | TextPromptBlockYAML
  | DownloadToS3BlockYAML
  | UploadToS3BlockYAML
  | FileUploadBlockYAML
  | SendEmailBlockYAML
  | FileUrlParserBlockYAML
  | ForLoopBlockYAML
  | ConditionalBlockYAML
  | ValidationBlockYAML
  | HumanInteractionBlockYAML
  | ActionBlockYAML
  | NavigationBlockYAML
  | ExtractionBlockYAML
  | LoginBlockYAML
  | WaitBlockYAML
  | FileDownloadBlockYAML
  | PDFParserBlockYAML
  | Taskv2BlockYAML
  | URLBlockYAML
  | HttpRequestBlockYAML;

export type BlockYAMLBase = {
  block_type: WorkflowBlockType;
  label: string;
  continue_on_failure?: boolean;
  next_loop_on_failure?: boolean;
  next_block_label?: string | null;
};

export type TaskBlockYAML = BlockYAMLBase & {
  block_type: "task";
  url: string | null;
  title?: string;
  navigation_goal: string | null;
  data_extraction_goal: string | null;
  data_schema: Record<string, unknown> | string | null;
  error_code_mapping: Record<string, string> | null;
  max_retries?: number;
  max_steps_per_run?: number | null;
  parameter_keys?: Array<string> | null;
  complete_on_download?: boolean;
  download_suffix?: string | null;
  totp_verification_url?: string | null;
  totp_identifier?: string | null;
  disable_cache: boolean;
  complete_criterion: string | null;
  terminate_criterion: string | null;
  include_action_history_in_verification: boolean;
  engine: RunEngine | null;
};

export type Taskv2BlockYAML = BlockYAMLBase & {
  block_type: "task_v2";
  url: string | null;
  prompt: string;
  totp_verification_url: string | null;
  totp_identifier: string | null;
  max_steps: number | null;
  disable_cache: boolean;
};

export type ValidationBlockYAML = BlockYAMLBase & {
  block_type: "validation";
  complete_criterion: string | null;
  terminate_criterion: string | null;
  error_code_mapping: Record<string, string> | null;
  parameter_keys?: Array<string> | null;
};

export type HumanInteractionBlockYAML = BlockYAMLBase & {
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

export type ActionBlockYAML = BlockYAMLBase & {
  block_type: "action";
  url: string | null;
  title?: string;
  navigation_goal: string | null;
  error_code_mapping: Record<string, string> | null;
  max_retries?: number;
  parameter_keys?: Array<string> | null;
  complete_on_download?: boolean;
  download_suffix?: string | null;
  totp_verification_url?: string | null;
  totp_identifier?: string | null;
  disable_cache: boolean;
  engine: RunEngine | null;
};

export type NavigationBlockYAML = BlockYAMLBase & {
  block_type: "navigation";
  url: string | null;
  title?: string;
  navigation_goal: string | null;
  error_code_mapping: Record<string, string> | null;
  max_retries?: number;
  max_steps_per_run?: number | null;
  parameter_keys?: Array<string> | null;
  complete_on_download?: boolean;
  download_suffix?: string | null;
  totp_verification_url?: string | null;
  totp_identifier?: string | null;
  disable_cache: boolean;
  complete_criterion: string | null;
  terminate_criterion: string | null;
  engine: RunEngine | null;
  model: WorkflowModel | null;
  include_action_history_in_verification: boolean;
};

export type ExtractionBlockYAML = BlockYAMLBase & {
  block_type: "extraction";
  url: string | null;
  title?: string;
  data_extraction_goal: string | null;
  data_schema: Record<string, unknown> | string | null;
  max_retries?: number;
  max_steps_per_run?: number | null;
  parameter_keys?: Array<string> | null;
  disable_cache: boolean;
  engine: RunEngine | null;
};

export type LoginBlockYAML = BlockYAMLBase & {
  block_type: "login";
  url: string | null;
  title?: string;
  navigation_goal: string | null;
  error_code_mapping: Record<string, string> | null;
  max_retries?: number;
  max_steps_per_run?: number | null;
  parameter_keys?: Array<string> | null;
  totp_verification_url?: string | null;
  totp_identifier?: string | null;
  disable_cache: boolean;
  complete_criterion: string | null;
  terminate_criterion: string | null;
  engine: RunEngine | null;
};

export type WaitBlockYAML = BlockYAMLBase & {
  block_type: "wait";
  wait_sec?: number;
};

export type FileDownloadBlockYAML = BlockYAMLBase & {
  block_type: "file_download";
  url: string | null;
  title?: string;
  navigation_goal: string | null;
  error_code_mapping: Record<string, string> | null;
  max_retries?: number;
  max_steps_per_run?: number | null;
  parameter_keys?: Array<string> | null;
  download_suffix?: string | null;
  totp_verification_url?: string | null;
  totp_identifier?: string | null;
  disable_cache: boolean;
  engine: RunEngine | null;
  download_timeout?: number | null;
};

export type CodeBlockYAML = BlockYAMLBase & {
  block_type: "code";
  code: string;
  parameter_keys?: Array<string> | null;
};

export type TextPromptBlockYAML = BlockYAMLBase & {
  block_type: "text_prompt";
  llm_key: string;
  prompt: string;
  json_schema?: Record<string, unknown> | null;
  parameter_keys?: Array<string> | null;
};

export type DownloadToS3BlockYAML = BlockYAMLBase & {
  block_type: "download_to_s3";
  url: string;
};

export type UploadToS3BlockYAML = BlockYAMLBase & {
  block_type: "upload_to_s3";
  path?: string | null;
};

export type FileUploadBlockYAML = BlockYAMLBase & {
  block_type: "file_upload";
  path?: string | null;
  storage_type: string;
  s3_bucket: string;
  region_name: string;
  aws_access_key_id: string;
  aws_secret_access_key: string;
  azure_storage_account_name?: string | null;
  azure_storage_account_key?: string | null;
  azure_blob_container_name?: string | null;
};

export type SendEmailBlockYAML = BlockYAMLBase & {
  block_type: "send_email";

  smtp_host_secret_parameter_key?: string;
  smtp_port_secret_parameter_key?: string;
  smtp_username_secret_parameter_key?: string;
  smtp_password_secret_parameter_key?: string;

  sender: string;
  recipients: Array<string>;
  subject: string;
  body: string;
  file_attachments?: Array<string> | null;
};

export type FileUrlParserBlockYAML = BlockYAMLBase & {
  block_type: "file_url_parser";
  file_url: string;
  file_type: "csv" | "excel" | "pdf";
  json_schema?: Record<string, unknown> | null;
};

export type ForLoopBlockYAML = BlockYAMLBase & {
  block_type: "for_loop";
  loop_over_parameter_key?: string;
  loop_blocks: Array<BlockYAML>;
  loop_variable_reference: string | null;
  complete_if_empty: boolean;
};

export type BranchCriteriaYAML = {
  criteria_type: string;
  expression: string;
  description?: string | null;
};

export type BranchConditionYAML = {
  id: string;
  criteria: BranchCriteriaYAML | null;
  next_block_label: string | null;
  description?: string | null;
  is_default: boolean;
};

export type ConditionalBlockYAML = BlockYAMLBase & {
  block_type: "conditional";
  branch_conditions: Array<BranchConditionYAML>;
};

export type PDFParserBlockYAML = BlockYAMLBase & {
  block_type: "pdf_parser";
  file_url: string;
  json_schema: Record<string, unknown> | null;
};

export type URLBlockYAML = BlockYAMLBase & {
  block_type: "goto_url";
  url: string;
};

export type HttpRequestBlockYAML = BlockYAMLBase & {
  block_type: "http_request";
  method: string;
  url: string | null;
  headers: Record<string, string> | null;
  body: Record<string, unknown> | null;
  files?: Record<string, string> | null; // Dictionary mapping field names to file paths/URLs
  timeout: number;
  follow_redirects: boolean;
  parameter_keys?: Array<string> | null;
};
