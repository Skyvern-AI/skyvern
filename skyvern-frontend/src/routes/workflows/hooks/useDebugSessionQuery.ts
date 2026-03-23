import { getClient } from "@/api/AxiosClient";
import { DebugSessionApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";

interface Opts {
  workflowPermanentId?: string;
  enabled?: boolean;
  isRateLimited?: boolean;
}

function useDebugSessionQuery({
  workflowPermanentId,
  enabled,
  isRateLimited,
}: Opts) {
  const credentialGetter = useCredentialGetter();

  const baseEnabled =
    enabled !== undefined
      ? enabled && !!workflowPermanentId
      : !!workflowPermanentId;

  return useQuery<DebugSessionApiResponse>({
    queryKey: ["debugSession", workflowPermanentId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/debug-session/${workflowPermanentId}`)
        .then((response) => response.data);
    },
    enabled: baseEnabled && !isRateLimited,
    // Reduce polling frequency on errors
    retry: 3,
    retryDelay: 10000,
    refetchOnWindowFocus: false,
    // Don't keep retrying if in error state
    refetchInterval: (query) => {
      if (isRateLimited) return false;
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
