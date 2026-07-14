import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";

interface RunTagSuggestionsResponse {
  keys: Array<string>;
  values_by_key: Record<string, Array<string>>;
  labels: Array<string>;
}

type RunTagSuggestions = {
  keys: Array<string>;
  valuesByKey: Map<string, Array<string>>;
  labels: Array<string>;
};

// Mirrors useTagValuesQuery's Map-in-`select` approach so the transform is
// memoized and the returned Map reference is stable across renders.
function useRunTagSuggestionsQuery({
  enabled = true,
}: { enabled?: boolean } = {}) {
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);

  return useQuery<RunTagSuggestionsResponse, Error, RunTagSuggestions>({
    queryKey: getOrgScopedQueryKey(
      ["run-tag-suggestions"],
      activeOrgQueryKeyScope,
    ),
    enabled,
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get<RunTagSuggestionsResponse>("/run-tag-suggestions")
        .then((response) => response.data);
    },
    select: (data) => ({
      keys: data.keys,
      valuesByKey: new Map(Object.entries(data.values_by_key)),
      labels: data.labels,
    }),
  });
}

export { useRunTagSuggestionsQuery };
