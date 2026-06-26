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

export { useTagValuesQuery };
