import { getClient } from "@/api/AxiosClient";
import { BitwardenItemsApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useWorkflowScopeReadOnly } from "@/routes/workflows/editor/WorkflowScopeContext";
import { useQuery } from "@tanstack/react-query";

type QueryReturnType = BitwardenItemsApiResponse;
type UseQueryOptions = Omit<
  Parameters<typeof useQuery<QueryReturnType>>[0],
  "queryKey" | "queryFn"
>;

function useBitwardenItemsQuery(props: UseQueryOptions = {}) {
  const credentialGetter = useCredentialGetter();
  const scopeReadOnly = useWorkflowScopeReadOnly();

  return useQuery<BitwardenItemsApiResponse>({
    queryKey: ["bitwardenItems"],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get("/credentials/bitwarden/items")
        .then((res) => res.data as BitwardenItemsApiResponse);
    },
    // Bitwarden items change rarely; avoid refetching every time the panel remounts.
    staleTime: 60_000,
    ...props,
    enabled: props.enabled !== false && !scopeReadOnly,
  });
}

export { useBitwardenItemsQuery };
