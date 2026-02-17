import { getClient } from "@/api/AxiosClient";
import { DebugSessionApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";

interface Opts {
  workflowPermanentId?: string;
  enabled?: boolean;
}

function useDebugSessionQuery({ workflowPermanentId, enabled }: Opts) {
  const credentialGetter = useCredentialGetter();

  return useQuery<DebugSessionApiResponse>({
    queryKey: ["debugSession", workflowPermanentId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/debug-session/${workflowPermanentId}`)
        .then((response) => response.data);
    },
    enabled:
      enabled !== undefined
        ? enabled && !!workflowPermanentId
        : !!workflowPermanentId,
    // Reduce polling frequency on errors
    retry: 3,
    retryDelay: 10000,
    refetchOnWindowFocus: false,
    // Don't keep retrying if in error state
    refetchInterval: (query) => {
      // If query is in error state, poll much less frequently (30s)
      // Otherwise don't auto-refetch
      if (query.state.status === "error") {
        return 30000;
      }
      return false;
    },
  });
}

export { useDebugSessionQuery };
