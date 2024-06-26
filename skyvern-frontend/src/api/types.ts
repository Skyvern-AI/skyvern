export const ArtifactType = {
  Recording: "recording",
  ActionScreenshot: "screenshot_action",
  LLMScreenshot: "screenshot_llm",
  LLMResponseRaw: "llm_response",
  LLMResponseParsed: "llm_response_parsed",
  VisibleElementsTree: "visible_elements_tree",
  VisibleElementsTreeTrimmed: "visible_elements_tree_trimmed",
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
  request: {
    title: string | null;
    url: string;
    webhook_callback_url: string;
    navigation_goal: string | null;
    data_extraction_goal: string | null;
    navigation_payload: string | object; // stringified JSON
    error_code_mapping: null;
    proxy_location: string;
    extracted_information_schema: string | object;
  };
  task_id: string;
  status: Status;
  created_at: string; // ISO 8601
  modified_at: string; // ISO 8601
  extracted_information: unknown;
  screenshot_url: string | null;
  recording_url: string | null;
  failure_reason: string | null;
  errors: unknown[];
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

export type WorkflowParameter = {
  workflow_parameter_id: string;
  workflow_parameter_type?: string;
  key: string;
  description: string | null;
  workflow_id: string;
  parameter_type: "workflow"; // TODO other values
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
