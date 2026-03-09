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
