export const TaskStatus = {
  Created: "created",
  Running: "running",
  Failed: "failed",
  Terminated: "terminated",
  Completed: "completed",
} as const;

export type TaskStatus = (typeof TaskStatus)[keyof typeof TaskStatus];

export type TaskApiResponse = {
  title: string | null;
  url: string;
  webhook_callback_url: string;
  navigation_goal: string;
  data_extraction_goal: string;
  navigation_payload: string; // stringified JSON
  error_code_mapping: null;
  proxy_location: "NONE";
  extracted_information_schema: string; // stringified JSON
  created_at: string; // ISO 8601
  modified_at: string; // ISO 8601
  task_id: string; // tsk_<numbers>
  status: TaskStatus;
  extracted_information: null; // ??
  failure_reason: null; // ??
  organization_id: string; // o_<numbers>
  workflow_run_id: null; // ?
  order: null; // ?
  retry: null; // ?
  errors: []; // ?
};
