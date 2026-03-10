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
