import { getClient } from "@/api/AxiosClient";
import { OnePasswordItemsApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useWorkflowScopeReadOnly } from "@/routes/workflows/editor/WorkflowScopeContext";
import { useQuery } from "@tanstack/react-query";

type QueryReturnType = OnePasswordItemsApiResponse;
type UseQueryOptions = Omit<
  Parameters<typeof useQuery<QueryReturnType>>[0],
  "queryKey" | "queryFn"
>;

function useOnePasswordItemsQuery(props: UseQueryOptions = {}) {
  const credentialGetter = useCredentialGetter();
  const scopeReadOnly = useWorkflowScopeReadOnly();

  return useQuery<OnePasswordItemsApiResponse>({
    queryKey: ["onepasswordItems"],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get("/credentials/onepassword/items")
        .then((res) => res.data as OnePasswordItemsApiResponse);
    },
    // 1Password items change rarely; avoid refetching every time the panel remounts.
    staleTime: 60_000,
    ...props,
    enabled: props.enabled !== false && !scopeReadOnly,
  });
}

export { useOnePasswordItemsQuery };
