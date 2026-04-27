export type WorkflowSchedule = {
  workflow_schedule_id: string;
  organization_id: string;
  workflow_permanent_id: string;
  cron_expression: string;
  timezone: string;
  enabled: boolean;
  parameters: Record<string, unknown> | null;
  temporal_schedule_id: string | null;
  name: string | null;
  description: string | null;
  created_at: string;
  modified_at: string;
  deleted_at: string | null;
};

export type WorkflowScheduleResponse = {
  schedule: WorkflowSchedule;
  next_runs: Array<string>;
};

export type WorkflowScheduleListResponse = {
  schedules: Array<WorkflowSchedule>;
};

export type CreateScheduleRequest = {
  cron_expression: string;
  timezone: string;
  enabled?: boolean;
  parameters?: Record<string, unknown> | null;
  name?: string;
  description?: string;
};

export type UpdateScheduleRequest = {
  cron_expression: string;
  timezone: string;
  enabled: boolean;
  parameters?: Record<string, unknown> | null;
  name?: string;
  description?: string;
};

export type OrganizationScheduleItem = {
  workflow_schedule_id: string;
  organization_id: string;
  workflow_permanent_id: string;
  workflow_title: string;
  cron_expression: string;
  timezone: string;
  enabled: boolean;
  parameters: Record<string, unknown> | null;
  name: string | null;
  description: string | null;
  next_run: string | null;
  created_at: string;
  modified_at: string;
};

export type OrganizationScheduleListResponse = {
  schedules: OrganizationScheduleItem[];
  total_count: number;
  page: number;
  page_size: number;
};
