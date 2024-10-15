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
} as const;

export const Status = {
  Created: "created",
  Running: "running",
  Failed: "failed",
  Terminated: "terminated",
  Completed: "completed",
  Queued: "queued",
  TimedOut: "timed_out",
  Canceled: "canceled",
} as const;

export type Status = (typeof Status)[keyof typeof Status];

export type ArtifactType = (typeof ArtifactType)[keyof typeof ArtifactType];

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
  errors: Array<Record<string, unknown>>;
  max_steps_per_run: number | null;
  workflow_run_id: string | null;
};

export type CreateTaskRequest = {
  title: string | null;
  url: string;
  webhook_callback_url: string | null;
  navigation_goal: string | null;
  data_extraction_goal: string | null;
  navigation_payload: Record<string, unknown> | string | null;
  extracted_information_schema: Record<string, unknown> | string | null;
  error_code_mapping: Record<string, string> | null;
  proxy_location: string | null;
  totp_verification_url: string | null;
  totp_identifier: string | null;
};

export type User = {
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

export type WorkflowParameter = {
  key: string;
  description: string | null;
  workflow_parameter_id: string;
  parameter_type: WorkflowParameterType;
  workflow_parameter_type: WorkflowParameterValueType;
  workflow_id: string;
  default_value?: string;
  created_at: string | null;
  modified_at: string | null;
  deleted_at: string | null;
};

export type WorkflowBlock = {
  label: string;
  block_type: string;
  output_parameter?: null;
  continue_on_failure: boolean;
  url: string;
  title: string;
  navigation_goal: string;
  data_extraction_goal: string;
  data_schema: object | null;
  error_code_mapping: null; // ?
  max_retries: number | null;
  max_steps_per_run: number | null;
  parameters: []; // ?
};

export type WorkflowApiResponse = {
  workflow_id: string;
  organization_id: string;
  is_saved_task: boolean;
  title: string;
  workflow_permanent_id: string;
  version: number;
  description: string;
  workflow_definition: {
    parameters: Array<WorkflowParameter>;
    blocks: Array<WorkflowBlock>;
  };
  proxy_location: string;
  webhook_callback_url: string;
  created_at: string;
  modified_at: string;
  deleted_at: string | null;
};

// TODO complete this
export const ActionTypes = {
  InputText: "input_text",
  Click: "click",
  SelectOption: "select_option",
  UploadFile: "upload_file",
  complete: "complete",
  wait: "wait",
  terminate: "terminate",
} as const;

export type ActionType = (typeof ActionTypes)[keyof typeof ActionTypes];

export const ReadableActionTypes: {
  [key in ActionType]: string;
} = {
  input_text: "Input Text",
  click: "Click",
  select_option: "Select Option",
  upload_file: "Upload File",
  complete: "Complete",
  wait: "Wait",
  terminate: "Terminate",
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
};

export type Action = {
  reasoning: string;
  confidence?: number;
  type: ActionType;
  input: string;
  success: boolean;
  stepId: string;
  index: number;
};

export type WorkflowRunApiResponse = {
  workflow_permanent_id: string;
  workflow_run_id: string;
  workflow_id: string;
  status: Status;
  proxy_location: string;
  webhook_callback_url: string;
  created_at: string;
  modified_at: string;
};

export type WorkflowRunStatusApiResponse = {
  workflow_id: string;
  workflow_run_id: string;
  status: Status;
  proxy_location: string;
  webhook_callback_url: string | null;
  created_at: string;
  modified_at: string;
  parameters: Record<string, unknown>;
  screenshot_urls: Array<string> | null;
  recording_url: string | null;
  outputs: Record<string, unknown> | null;
};

export type TaskGenerationApiResponse = {
  suggested_title: string | null;
  url: string | null;
  navigation_goal: string | null;
  data_extraction_goal: string | null;
  navigation_payload: Record<string, unknown> | null;
  extracted_information_schema: Record<string, unknown> | null;
};
