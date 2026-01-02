import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";

export const ArtifactType = {
  Recording: "recording",
  ActionScreenshot: "screenshot_action",
  LLMScreenshot: "screenshot_llm",
  LLMResponseRaw: "llm_response",
  LLMResponseParsed: "llm_response_parsed",
  VisibleElementsTree: "visible_elements_tree",
  VisibleElementsTreeTrimmed: "visible_elements_tree_trimmed",
  VisibleElementsTreeInPrompt: "visible_elements_tree_in_prompt",
  LLMPrompt: "llm_prompt",
  LLMRequest: "llm_request",
  HTMLScrape: "html_scrape",
  SkyvernLog: "skyvern_log",
  SkyvernLogRaw: "skyvern_log_raw",
} as const;

export type ArtifactType = (typeof ArtifactType)[keyof typeof ArtifactType];

export const Status = {
  Created: "created",
  Running: "running",
  Failed: "failed",
  Terminated: "terminated",
  Completed: "completed",
  Queued: "queued",
  TimedOut: "timed_out",
  Canceled: "canceled",
  Skipped: "skipped",
  Paused: "paused",
} as const;

export type Status = (typeof Status)[keyof typeof Status];

export const ProxyLocation = {
  Residential: "RESIDENTIAL",
  ResidentialIE: "RESIDENTIAL_IE",
  ResidentialES: "RESIDENTIAL_ES",
  ResidentialIN: "RESIDENTIAL_IN",
  ResidentialJP: "RESIDENTIAL_JP",
  ResidentialGB: "RESIDENTIAL_GB",
  ResidentialFR: "RESIDENTIAL_FR",
  ResidentialDE: "RESIDENTIAL_DE",
  ResidentialNZ: "RESIDENTIAL_NZ",
  ResidentialZA: "RESIDENTIAL_ZA",
  ResidentialAR: "RESIDENTIAL_AR",
  ResidentialAU: "RESIDENTIAL_AU",
  ResidentialBR: "RESIDENTIAL_BR",
  ResidentialTR: "RESIDENTIAL_TR",
  ResidentialCA: "RESIDENTIAL_CA",
  ResidentialMX: "RESIDENTIAL_MX",
  ResidentialIT: "RESIDENTIAL_IT",
  ResidentialNL: "RESIDENTIAL_NL",
  ResidentialISP: "RESIDENTIAL_ISP",
  None: "NONE",
} as const;

export type LegacyProxyLocation =
  (typeof ProxyLocation)[keyof typeof ProxyLocation];

export type GeoTarget = {
  country: string;
  subdivision?: string;
  city?: string;
  isISP?: boolean;
};

export type ProxyLocation = LegacyProxyLocation | GeoTarget | null;

export type ArtifactApiResponse = {
  created_at: string;
  modified_at: string;
  artifact_id: string;
  task_id: string;
  step_id: string;
  artifact_type: ArtifactType;
  uri: string;
  signed_url?: string | null;
  organization_id: string;
};

export type ActionResultApiResponse = {
  success: boolean;
};

export type ActionAndResultApiResponse = [
  ActionApiResponse,
  Array<ActionResultApiResponse>,
];

export type StepApiResponse = {
  step_id: string;
  task_id: string;
  created_at: string;
  modified_at: string;
  input_token_count: number;
  is_last: boolean;
  order: number;
  organization_id: string;
  output?: {
    actions_and_results: ActionAndResultApiResponse[];
    errors: unknown[];
  };
  retry_index: number;
  status: Status;
  step_cost: number;
};

export type Task = {
  task_id: string;
  status: Status;
  created_at: string; // ISO 8601
  modified_at: string; // ISO 8601
  extracted_information: Record<string, unknown> | string | null;
  screenshot_url: string | null;
  recording_url: string | null;
  organization_id: string;
  workflow_run_id: string | null;
  order: number | null;
  retry: number | null;
  max_steps_per_run: number | null;
  errors: Array<Record<string, unknown>>;
  title: string | null;
  url: string;
  webhook_callback_url: string | null;
  webhook_failure_reason: string | null;
  navigation_goal: string | null;
  data_extraction_goal: string | null;
  navigation_payload: Record<string, unknown> | string | null;
  complete_criterion: string | null;
  terminate_criterion: string | null;
  application: string | null;
};

export type TaskApiResponse = {
  request: CreateTaskRequest;
  task_id: string;
  status: Status;
  created_at: string; // ISO 8601
  modified_at: string; // ISO 8601
  extracted_information: Record<string, unknown> | string | null;
  screenshot_url: string | null;
  recording_url: string | null;
  failure_reason: string | null;
  webhook_failure_reason: string | null;
  errors: Array<Record<string, unknown>>;
  max_steps_per_run: number | null;
  task_v2: TaskV2 | null;
  workflow_run_id: string | null;
};

export type CreateTaskRequest = {
  title?: string | null;
  url: string;
  webhook_callback_url?: string | null;
  navigation_goal?: string | null;
  data_extraction_goal?: string | null;
  navigation_payload?: Record<string, unknown> | string | null;
  extracted_information_schema?: Record<string, unknown> | string | null;
  extra_http_headers?: Record<string, string> | null;
  error_code_mapping?: Record<string, string> | null;
  proxy_location?: ProxyLocation | null;
  totp_verification_url?: string | null;
  totp_identifier?: string | null;
  application?: string | null;
  include_action_history_in_verification?: boolean | null;
  max_screenshot_scrolls?: number | null;
  browser_address?: string | null;
};

export type User = {
  id: string;
  email: string;
  name: string;
};

export type OrganizationApiResponse = {
  created_at: string;
  modified_at: string;
  max_retries_per_step: number | null;
  max_steps_per_run: number | null;
  organization_id: string;
  organization_name: string;
  webhook_callback_url: string | null;
};

export type ApiKeyApiResponse = {
  id: string;
  organization_id: string;
  token: string;
  created_at: string;
  modified_at: string;
  token_type: string;
  valid: boolean;
};

export type OnePasswordTokenApiResponse = {
  id: string;
  organization_id: string;
  token: string;
  created_at: string;
  modified_at: string;
  token_type: string;
  valid: boolean;
};

export type CreateOnePasswordTokenRequest = {
  token: string;
};

export type CreateOnePasswordTokenResponse = {
  token: OnePasswordTokenApiResponse;
};

export interface AzureClientSecretCredential {
  tenant_id: string;
  client_id: string;
  client_secret: string;
}

export interface AzureOrganizationAuthToken {
  id: string;
  organization_id: string;
  credential: AzureClientSecretCredential;
  created_at: string;
  modified_at: string;
  token_type: string;
  valid: boolean;
}

export interface CreateAzureClientSecretCredentialRequest {
  credential: AzureClientSecretCredential;
}

export interface AzureClientSecretCredentialResponse {
  token: AzureOrganizationAuthToken;
}

export interface CustomCredentialServiceConfig {
  api_base_url: string;
  api_token: string;
}

export interface CustomCredentialServiceOrganizationAuthToken {
  id: string;
  organization_id: string;
  token: string; // JSON string containing CustomCredentialServiceConfig
  created_at: string;
  modified_at: string;
  token_type: string;
  valid: boolean;
}

export interface CreateCustomCredentialServiceConfigRequest {
  config: CustomCredentialServiceConfig;
}

export interface CustomCredentialServiceConfigResponse {
  token: CustomCredentialServiceOrganizationAuthToken;
}

// TODO complete this
export const ActionTypes = {
  InputText: "input_text",
  Click: "click",
  Hover: "hover",
  SelectOption: "select_option",
  UploadFile: "upload_file",
  DownloadFile: "download_file",
  complete: "complete",
  wait: "wait",
  terminate: "terminate",
  SolveCaptcha: "solve_captcha",
  extract: "extract",
  ReloadPage: "reload_page",
  KeyPress: "keypress",
  Scroll: "scroll",
  Move: "move",
  NullAction: "null_action",
  VerificationCode: "verification_code",
  Drag: "drag",
  LeftMouse: "left_mouse",
  GotoUrl: "goto_url",
  ClosePage: "close_page",
} as const;

export type ActionType = (typeof ActionTypes)[keyof typeof ActionTypes];

export const ReadableActionTypes: {
  [key in ActionType]: string;
} = {
  input_text: "Input Text",
  click: "Click",
  hover: "Hover",
  select_option: "Select Option",
  upload_file: "Upload File",
  download_file: "Download File",
  complete: "Complete",
  wait: "Wait",
  terminate: "Terminate",
  solve_captcha: "Solve Captcha",
  extract: "Extract Data",
  reload_page: "Reload Page",
  keypress: "Press Keys",
  scroll: "Scroll",
  move: "Move",
  null_action: "Screenshot",
  verification_code: "Verification Code",
  drag: "Drag",
  left_mouse: "Left Mouse",
  goto_url: "Goto URL",
  close_page: "Close Page",
};

export type Option = {
  label: string;
  index: number;
  value: string;
};

export type ActionApiResponse = {
  reasoning: string;
  confidence_float?: number;
  action_type: ActionType;
  text: string | null;
  option: Option | null;
  file_url: string | null;
  created_by: string | null;
};

export type Action = {
  reasoning: string;
  confidence?: number;
  type: ActionType;
  input: string;
  success: boolean;
  stepId: string;
  index: number;
  created_by: string | null;
};

export type EvalKind = "workflow" | "task";

export interface Eval {
  kind: EvalKind;
  created_at: string;
  organization_id: string;
  status: Status;
  title: string | null;
  workflow_permanent_id: string | null;
  workflow_run_id: string | null;
}

export interface EvalWorkflow extends Eval {
  kind: "workflow";
}

export interface EvalTask extends Eval {
  kind: "task";
  task_id: string;
  url: string | null;
}

export type EvalApiResponse = EvalWorkflow[] | EvalTask[];

export type DebugSessionApiResponse = {
  debug_session_id: string;
  browser_session_id: string;
  workflow_permanent_id: string | null;
  created_at: string;
  modified_at: string;
  vnc_streaming_supported: boolean | null;
};

export type WorkflowRunApiResponse = {
  created_at: string;
  failure_reason: string | null;
  modified_at: string;
  proxy_location: ProxyLocation | null;
  script_run: boolean | null;
  status: Status;
  title?: string;
  webhook_callback_url: string;
  workflow_id: string;
  workflow_permanent_id: string;
  workflow_run_id: string;
  workflow_title: string | null;
};

export type WorkflowRunStatusApiResponse = {
  workflow_id: string;
  workflow_run_id: string;
  status: Status;
  proxy_location: ProxyLocation | null;
  webhook_callback_url: string | null;
  extra_http_headers: Record<string, string> | null;
  created_at: string;
  finished_at: string;
  modified_at: string;
  parameters: Record<string, unknown>;
  screenshot_urls: Array<string> | null;
  recording_url: string | null;
  outputs: Record<string, unknown> | null;
  failure_reason: string | null;
  webhook_failure_reason: string | null;
  downloaded_file_urls: Array<string> | null;
  total_steps: number | null;
  total_cost: number | null;
  task_v2: TaskV2 | null;
  workflow_title: string | null;
  browser_session_id: string | null;
  max_screenshot_scrolls: number | null;
};

export type WorkflowRunStatusApiResponseWithWorkflow = {
  workflow_id: string;
  workflow_run_id: string;
  status: Status;
  proxy_location: ProxyLocation | null;
  webhook_callback_url: string | null;
  extra_http_headers: Record<string, string> | null;
  created_at: string;
  finished_at: string;
  modified_at: string;
  parameters: Record<string, unknown>;
  screenshot_urls: Array<string> | null;
  recording_url: string | null;
  outputs: Record<string, unknown> | null;
  failure_reason: string | null;
  webhook_failure_reason: string | null;
  downloaded_file_urls: Array<string> | null;
  total_steps: number | null;
  total_cost: number | null;
  task_v2: TaskV2 | null;
  workflow_title: string | null;
  browser_session_id: string | null;
  max_screenshot_scrolls: number | null;
  workflow: WorkflowApiResponse;
};

export type TaskGenerationApiResponse = {
  suggested_title: string | null;
  url: string | null;
  navigation_goal: string | null;
  data_extraction_goal: string | null;
  navigation_payload: Record<string, unknown> | null;
  extracted_information_schema: Record<string, unknown> | null;
};

export type ActionsApiResponse = {
  action_id: string;
  action_type: ActionType;
  status: Status;
  task_id: string | null;
  step_id: string | null;
  step_order: number | null;
  action_order: number | null;
  confidence_float: number | null;
  description: string | null;
  reasoning: string | null;
  intention: string | null;
  response: string | null;
  created_by: string | null;
  text: string | null;
};

export type TaskV2 = {
  task_id: string;
  status: Status;
  workflow_run_id: string | null;
  workflow_id: string | null;
  workflow_permanent_id: string | null;
  prompt: string | null;
  url: string | null;
  created_at: string;
  modified_at: string;
  output: Record<string, unknown> | null;
  summary: string | null;
  webhook_callback_url: string | null;
  webhook_failure_reason: string | null;
  totp_verification_url: string | null;
  totp_identifier: string | null;
  proxy_location: ProxyLocation | null;
  extra_http_headers: Record<string, string> | null;
};

export type Createv2TaskRequest = {
  user_prompt: string;
  webhook_callback_url?: string | null;
  proxy_location?: ProxyLocation | null;
  browser_session_id?: string | null;
};

export type PasswordCredentialApiResponse = {
  username: string;
  totp_type: "authenticator" | "email" | "text" | "none";
};

export type CreditCardCredentialApiResponse = {
  last_four: string;
  brand: string;
};

export type SecretCredentialResponse = {
  secret_label?: string | null;
};

export type CredentialApiResponse = {
  credential_id: string;
  credential:
    | PasswordCredentialApiResponse
    | CreditCardCredentialApiResponse
    | SecretCredentialResponse;
  credential_type: "password" | "credit_card" | "secret";
  name: string;
};

export function isPasswordCredential(
  credential:
    | PasswordCredentialApiResponse
    | CreditCardCredentialApiResponse
    | SecretCredentialResponse,
): credential is PasswordCredentialApiResponse {
  return "username" in credential;
}

export function isCreditCardCredential(
  credential:
    | PasswordCredentialApiResponse
    | CreditCardCredentialApiResponse
    | SecretCredentialResponse,
): credential is CreditCardCredentialApiResponse {
  return "last_four" in credential;
}

export function isSecretCredential(
  credential:
    | PasswordCredentialApiResponse
    | CreditCardCredentialApiResponse
    | SecretCredentialResponse,
): credential is SecretCredentialResponse {
  return !("username" in credential) && !("last_four" in credential);
}

export type CreateCredentialRequest = {
  name: string;
  credential_type: "password" | "credit_card" | "secret";
  credential: PasswordCredential | CreditCardCredential | SecretCredential;
};

export type PasswordCredential = {
  username: string;
  password: string;
  totp: string | null;
  totp_type: "authenticator" | "email" | "text" | "none";
  totp_identifier?: string | null;
};

export type CreditCardCredential = {
  card_number: string;
  card_cvv: string;
  card_exp_month: string;
  card_exp_year: string;
  card_brand: string;
  card_holder_name: string;
};

export type SecretCredential = {
  secret_value: string;
  secret_label?: string | null;
};

export const OtpType = {
  Totp: "totp",
  MagicLink: "magic_link",
} as const;

export type OtpType = (typeof OtpType)[keyof typeof OtpType];

export type TotpCode = {
  totp_code_id: string;
  totp_identifier: string | null;
  code: string;
  content: string;
  workflow_run_id: string | null;
  workflow_id: string | null;
  task_id: string | null;
  source: string | null;
  otp_type: OtpType | null;
  expired_at: string | null;
  created_at: string;
  modified_at: string;
};

export type TotpCodeListParams = {
  totp_identifier?: string;
  workflow_run_id?: string;
  otp_type?: OtpType;
  limit?: number;
};

export type ModelsResponse = {
  models: Record<string, string>;
};

export const RunEngine = {
  SkyvernV1: "skyvern-1.0",
  SkyvernV2: "skyvern-2.0",
  OpenaiCua: "openai-cua",
  AnthropicCua: "anthropic-cua",
} as const;

export type RunEngine = (typeof RunEngine)[keyof typeof RunEngine];

export type PylonEmailHash = {
  hash: string;
};

export const BROWSER_DOWNLOAD_TIMEOUT_SECONDS = 120 as const;
