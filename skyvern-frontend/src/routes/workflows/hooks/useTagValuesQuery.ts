import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import type { TagValue } from "../types/tagTypes";
import { buildTagColorMap, type TagColorMap } from "../types/tagColors";

// Per-org grouped-tag color registry. Returns a (key, value) -> palette color Map
// (built in `select`, so the transform is memoized and the map reference is stable
// across renders). Mirrors useTagKeysQuery's join of the key registry.
function useTagValuesQuery({ enabled = true }: { enabled?: boolean } = {}) {
  const credentialGetter = useCredentialGetter();

  return useQuery<Array<TagValue>, Error, TagColorMap>({
    queryKey: ["tag-values"],
    enabled,
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get<Array<TagValue>>("/tag-values")
        .then((response) => response.data);
    },
    select: buildTagColorMap,
  });
}

// Same source/cache as useTagValuesQuery but returns the raw rows (with
// workflow_count) for the label-management surface, which needs usage counts and
// per-(key,value) actions rather than just the color lookup map.
function useTagValuesListQuery({ enabled = true }: { enabled?: boolean } = {}) {
  const credentialGetter = useCredentialGetter();

  return useQuery<Array<TagValue>>({
    queryKey: ["tag-values"],
    enabled,
    // This queryFn intentionally duplicates useTagValuesQuery's so the shared
    // "tag-values" key still has a fetcher when only this hook is mounted (e.g.
    // direct nav to /settings/labels); removing it yields a Missing queryFn error.
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get<Array<TagValue>>("/tag-values")
        .then((response) => response.data);
    },
  });
}

export { useTagValuesQuery, useTagValuesListQuery };
