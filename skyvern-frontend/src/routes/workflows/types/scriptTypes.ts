export type ScriptBlocksResponse = {
  blocks: { [blockName: string]: string };
  main_script: string | null;
  script_id: string | null;
  version: number | null;
};

export type CacheKeyValuesResponse = {
  filtered_count: number;
  page: number;
  page_size: number;
  total_count: number;
  values: string[];
};

export type ScriptVersionSummary = {
  version: number;
  script_revision_id: string;
  created_at: string;
  run_id: string | null;
};

export type ScriptVersionListResponse = {
  versions: ScriptVersionSummary[];
};

export type ScriptVersionDetailResponse = {
  script_id: string;
  script_revision_id: string;
  version: number;
  created_at: string;
  run_id: string | null;
  blocks: { [blockName: string]: string };
  main_script: string | null;
  fallback_episode_count: number;
};

export type ScriptFallbackEpisode = {
  episode_id: string;
  organization_id: string;
  workflow_permanent_id: string;
  workflow_run_id: string;
  script_revision_id: string | null;
  block_label: string;
  fallback_type: "element" | "full_block" | "conditional_agent";
  error_message: string | null;
  classify_result: string | null;
  agent_actions: unknown[] | Record<string, unknown> | null;
  page_url: string | null;
  page_text_snapshot: string | null;
  fallback_succeeded: boolean | null;
  reviewed: boolean;
  reviewer_output: string | null;
  new_script_revision_id: string | null;
  created_at: string;
  modified_at: string;
};

export type FallbackEpisodeListResponse = {
  episodes: ScriptFallbackEpisode[];
  page: number;
  page_size: number;
  total_count: number;
};

export type ScriptVersionCompareResponse = {
  script_id: string;
  base_version: number;
  base_blocks: { [blockName: string]: string };
  base_main_script: string | null;
  base_created_at: string;
  base_run_id: string | null;
  compare_version: number;
  compare_blocks: { [blockName: string]: string };
  compare_main_script: string | null;
  compare_created_at: string;
  compare_run_id: string | null;
};

export type ReviewScriptRequest = {
  user_instructions: string;
  workflow_run_id?: string | null;
};

export type ReviewScriptResponse = {
  script_id: string;
  version: number;
  updated_blocks: string[];
  message?: string | null;
};

export type WorkflowScriptSummary = {
  script_id: string;
  cache_key: string;
  cache_key_value: string;
  // Must match ScriptStatus enum in skyvern/schemas/scripts.py
  status: "published" | "pending";
  latest_version: number;
  version_count: number;
  total_runs: number;
  success_rate: number | null;
  is_pinned: boolean;
  created_at: string;
  modified_at: string;
};

export type WorkflowScriptsListResponse = {
  scripts: WorkflowScriptSummary[];
};

export type PinScriptRequest = {
  cache_key_value: string;
};

export type PinScriptResponse = {
  workflow_permanent_id: string;
  cache_key_value: string;
  is_pinned: boolean;
  pinned_at: string | null;
};

export type ScriptRunSummary = {
  workflow_run_id: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  failure_reason: string | null;
};

export type ScriptRunsResponse = {
  runs: ScriptRunSummary[];
  total_count: number;
  status_counts: Record<string, number>;
};
