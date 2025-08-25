export type ScriptBlocksResponse = {
  blocks: { [blockName: string]: string };
};

export type CacheKeyValuesResponse = {
  filtered_count: number;
  page: number;
  page_size: number;
  total_count: number;
  values: string[];
};
