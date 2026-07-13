import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import type { TagValue } from "../types/tagTypes";
import { buildTagColorMap, type TagColorMap } from "../types/tagColors";

type TagValuesQueryOptions = {
  enabled?: boolean;
  key?: string | null;
};

function tagValuesQueryOptions(key: string | null | undefined) {
  const trimmedKey = key?.trim();
  const params = new URLSearchParams();
  if (trimmedKey) {
    params.append("key", trimmedKey);
  }
  return {
    queryKey: trimmedKey ? ["tag-values", { key: trimmedKey }] : ["tag-values"],
    requestConfig: trimmedKey ? { params } : undefined,
  };
}

// Per-org grouped-tag color registry. Returns a (key, value) -> palette color Map
// (built in `select`, so the transform is memoized and the map reference is stable
// across renders). Mirrors useTagKeysQuery's join of the key registry.
function useTagValuesQuery({
  enabled = true,
  key = null,
}: TagValuesQueryOptions = {}) {
  const credentialGetter = useCredentialGetter();
  const { queryKey, requestConfig } = tagValuesQueryOptions(key);

  return useQuery<Array<TagValue>, Error, TagColorMap>({
    queryKey,
    enabled,
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const request = requestConfig
        ? client.get<Array<TagValue>>("/tag-values", requestConfig)
        : client.get<Array<TagValue>>("/tag-values");
      return request.then((response) => response.data);
    },
    select: buildTagColorMap,
  });
}

// Same source/cache as useTagValuesQuery but returns the raw rows (with
// workflow_count) for the label-management surface, which needs usage counts and
// per-(key,value) actions rather than just the color lookup map.
function useTagValuesListQuery({
  enabled = true,
  key = null,
}: TagValuesQueryOptions = {}) {
  const credentialGetter = useCredentialGetter();
  const { queryKey, requestConfig } = tagValuesQueryOptions(key);

  return useQuery<Array<TagValue>>({
    queryKey,
    enabled,
    // This queryFn intentionally duplicates useTagValuesQuery's so the shared
    // "tag-values" key still has a fetcher when only this hook is mounted (e.g.
    // direct nav to /settings/labels); removing it yields a Missing queryFn error.
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const request = requestConfig
        ? client.get<Array<TagValue>>("/tag-values", requestConfig)
        : client.get<Array<TagValue>>("/tag-values");
      return request.then((response) => response.data);
    },
  });
}

export { useTagValuesQuery, useTagValuesListQuery };
