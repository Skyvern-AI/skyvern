import { getClient } from "@/api/AxiosClient";
import { DebugSessionApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";

const DEBUG_SESSION_KEEP_ALIVE_INTERVAL_MS = 5 * 60 * 1000;
const DEBUG_SESSION_ERROR_REFETCH_INTERVAL_MS = 30 * 1000;

type DebugSessionRefetchState = {
  status: "pending" | "error" | "success";
  data?: { browser_session_id?: string | null } | null;
};

function getDebugSessionRefetchInterval(
  queryState: DebugSessionRefetchState,
  isRateLimited = false,
  keepAliveBrowserSession = false,
): number | false {
  if (isRateLimited) {
    return false;
  }
  if (queryState.status === "error") {
    return DEBUG_SESSION_ERROR_REFETCH_INTERVAL_MS;
  }
  if (keepAliveBrowserSession && queryState.data?.browser_session_id) {
    return DEBUG_SESSION_KEEP_ALIVE_INTERVAL_MS;
  }
  return false;
}

interface Opts {
  workflowPermanentId?: string;
  enabled?: boolean;
  isRateLimited?: boolean;
  keepAliveBrowserSession?: boolean;
}

function useDebugSessionQuery({
  workflowPermanentId,
  enabled,
  isRateLimited,
  keepAliveBrowserSession,
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
    refetchInterval: (query) =>
      getDebugSessionRefetchInterval(
        query.state,
        isRateLimited,
        keepAliveBrowserSession,
      ),
    // Keep lease renewal polling active even when the editor tab is backgrounded.
    refetchIntervalInBackground: keepAliveBrowserSession,
  });
}

export {
  DEBUG_SESSION_ERROR_REFETCH_INTERVAL_MS,
  DEBUG_SESSION_KEEP_ALIVE_INTERVAL_MS,
  getDebugSessionRefetchInterval,
  useDebugSessionQuery,
};
