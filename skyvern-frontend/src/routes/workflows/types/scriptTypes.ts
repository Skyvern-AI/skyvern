export type ScriptBlocksResponse = {
  blocks: { [blockName: string]: string };
  main_script: string | null;
};

export type CacheKeyValuesResponse = {
  filtered_count: number;
  page: number;
  page_size: number;
  total_count: number;
  values: string[];
};
