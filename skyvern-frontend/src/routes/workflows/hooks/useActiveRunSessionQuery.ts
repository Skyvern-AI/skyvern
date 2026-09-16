import { getClient } from "@/api/AxiosClient";
import { isForbiddenError } from "@/api/forbidden";
import { DebugSessionViewerStateApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

const ACTIVE_RUN_SESSION_REFETCH_INTERVAL_MS = 1000;

type ActiveRunSessionRefetchState = {
  status?: "pending" | "error" | "success";
  data?: DebugSessionViewerStateApiResponse;
  error?: unknown;
};

function getActiveRunSessionRefetchInterval(
  queryState: ActiveRunSessionRefetchState,
  isTurnActive: boolean,
): number | false {
  // A forbidden viewer-state (expired session or another org's workflow) never
  // becomes allowed on its own, so stop polling instead of hammering it 1/s.
  if (queryState.status === "error" && isForbiddenError(queryState.error)) {
    return false;
  }
  return isTurnActive || Boolean(queryState.data?.active_run_session_id)
    ? ACTIVE_RUN_SESSION_REFETCH_INTERVAL_MS
    : false;
}

interface Opts {
  workflowPermanentId?: string;
  enabled?: boolean;
  isTurnActive: boolean;
}

function useActiveRunSessionQuery({
  workflowPermanentId,
  enabled = true,
  isTurnActive,
}: Opts) {
  const credentialGetter = useCredentialGetter();
  const query = useQuery<DebugSessionViewerStateApiResponse>({
    queryKey: ["debugSessionViewerState", workflowPermanentId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.get<DebugSessionViewerStateApiResponse>(
        `/debug-session/${workflowPermanentId}/viewer-state`,
      );
      return response.data;
    },
    enabled: enabled && Boolean(workflowPermanentId),
    refetchInterval: (activeQuery) =>
      getActiveRunSessionRefetchInterval(activeQuery.state, isTurnActive),
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
  });
  const priorTurnActive = useRef(isTurnActive);

  useEffect(() => {
    if (priorTurnActive.current !== isTurnActive && enabled) {
      void query.refetch();
    }
    priorTurnActive.current = isTurnActive;
  }, [enabled, isTurnActive, query]);

  return query;
}

export {
  ACTIVE_RUN_SESSION_REFETCH_INTERVAL_MS,
  getActiveRunSessionRefetchInterval,
  useActiveRunSessionQuery,
};
