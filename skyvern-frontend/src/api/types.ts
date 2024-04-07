export const ArtifactType = {
  Recording: "recording",
  ActionScreenshot: "screenshot_action",
} as const;

export const Status = {
  Created: "created",
  Running: "running",
  Failed: "failed",
  Terminated: "terminated",
  Completed: "completed",
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
    navigation_payload: string; // stringified JSON
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
