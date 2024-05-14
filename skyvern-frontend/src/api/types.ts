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

export type StepApiResponse = {
  step_id: string;
  task_id: string;
  created_at: string;
  modified_at: string;
  input_token_count: number;
  is_last: boolean;
  order: number;
  organization_id: string;
  output: {
    action_results: unknown[];
    actions_and_results: unknown[];
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
    navigation_goal: string;
    data_extraction_goal: string;
    navigation_payload: string | object; // stringified JSON
    error_code_mapping: null;
    proxy_location: string;
    extracted_information_schema: string;
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
