export type WorkflowCreateYAMLRequest = {
  title: string;
  description?: string | null;
  proxy_location?: string | null;
  webhook_callback_url?: string | null;
  totp_verification_url?: string | null;
  workflow_definition: WorkflowDefinitionYAML;
  is_saved_task?: boolean;
};

export type WorkflowDefinitionYAML = {
  parameters: Array<ParameterYAML>;
  blocks: Array<BlockYAML>;
};

export type ParameterYAML =
  | WorkflowParameterYAML
  | BitwardenLoginCredentialParameterYAML
  | AWSSecretParameterYAML
  | ContextParameterYAML
  | OutputParameterYAML
  | BitwardenSensitiveInformationParameterYAML;

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
  bitwarden_collection_id: string;
  url_parameter_key: string;
  bitwarden_client_id_aws_secret_key: "SKYVERN_BITWARDEN_CLIENT_ID";
  bitwarden_client_secret_aws_secret_key: "SKYVERN_BITWARDEN_CLIENT_SECRET";
  bitwarden_master_password_aws_secret_key: "SKYVERN_BITWARDEN_MASTER_PASSWORD";
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

export type ContextParameterYAML = ParameterYAMLBase & {
  parameter_type: "context";
  source_parameter_key: string;
};

export type OutputParameterYAML = ParameterYAMLBase & {
  parameter_type: "output";
};

const BlockTypes = {
  TASK: "task",
  FOR_LOOP: "for_loop",
  CODE: "code",
  TEXT_PROMPT: "text_prompt",
  DOWNLOAD_TO_S3: "download_to_s3",
  UPLOAD_TO_S3: "upload_to_s3",
  SEND_EMAIL: "send_email",
  FILE_URL_PARSER: "file_url_parser",
  VALIDATION: "validation",
  ACTION: "action",
  NAVIGATION: "navigation",
  EXTRACTION: "extraction",
} as const;

export type BlockType = (typeof BlockTypes)[keyof typeof BlockTypes];

export type BlockYAML =
  | TaskBlockYAML
  | CodeBlockYAML
  | TextPromptBlockYAML
  | DownloadToS3BlockYAML
  | UploadToS3BlockYAML
  | SendEmailBlockYAML
  | FileUrlParserBlockYAML
  | ForLoopBlockYAML
  | ValidationBlockYAML
  | ActionBlockYAML
  | NavigationBlockYAML
  | ExtractionBlockYAML;

export type BlockYAMLBase = {
  block_type: BlockType;
  label: string;
  continue_on_failure?: boolean;
};

export type TaskBlockYAML = BlockYAMLBase & {
  block_type: "task";
  url: string | null;
  title?: string;
  navigation_goal: string | null;
  data_extraction_goal: string | null;
  data_schema: Record<string, unknown> | null;
  error_code_mapping: Record<string, string> | null;
  max_retries?: number;
  max_steps_per_run?: number | null;
  parameter_keys?: Array<string> | null;
  complete_on_download?: boolean;
  download_suffix?: string | null;
  totp_verification_url?: string | null;
  totp_identifier?: string | null;
  cache_actions: boolean;
};

export type ValidationBlockYAML = BlockYAMLBase & {
  block_type: "validation";
  complete_criterion: string | null;
  terminate_criterion: string | null;
  error_code_mapping: Record<string, string> | null;
  parameter_keys?: Array<string> | null;
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
  cache_actions: boolean;
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
  cache_actions: boolean;
};

export type ExtractionBlockYAML = BlockYAMLBase & {
  block_type: "extraction";
  url: string | null;
  title?: string;
  data_extraction_goal: string | null;
  data_schema: Record<string, unknown> | null;
  max_retries?: number;
  max_steps_per_run?: number | null;
  parameter_keys?: Array<string> | null;
  cache_actions: boolean;
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
  file_type: "csv";
};

export type ForLoopBlockYAML = BlockYAMLBase & {
  block_type: "for_loop";
  loop_over_parameter_key: string;
  loop_blocks: Array<BlockYAML>;
};
