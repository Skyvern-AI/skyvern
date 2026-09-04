import { useEffect, useRef } from "react";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import {
  keepPreviousData,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import type { RunHealEpisodesResponse } from "../types/healTypes";
import { useWorkflowRunWithWorkflowQuery } from "./useWorkflowRunWithWorkflowQuery";

type UseRunHealEpisodesQueryOptions = {
  workflowRunId?: string;
  enabled?: boolean;
};

function useRunHealEpisodesQuery({
  workflowRunId,
  enabled = true,
}: UseRunHealEpisodesQueryOptions) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { data: workflowRun, dataUpdatedAt } = useWorkflowRunWithWorkflowQuery({
    workflowRunId,
  });

  // Follow the workflow-run query's polling: while a run is active it refetches
  // (~5s), so invalidate heal episodes on each update — a self-heal recorded
  // mid-run then surfaces without needing a remount or window refocus.
  const prevDataUpdatedAtRef = useRef<number>(dataUpdatedAt);
  useEffect(() => {
    if (dataUpdatedAt !== prevDataUpdatedAtRef.current && workflowRunId) {
      queryClient.invalidateQueries({
        queryKey: ["run-heal-episodes", workflowRunId],
      });
    }
    prevDataUpdatedAtRef.current = dataUpdatedAt;
  }, [dataUpdatedAt, workflowRunId, queryClient]);

  return useQuery<RunHealEpisodesResponse>({
    // Same reason the run timeline carries its status: the invalidation above only fires while the
    // run query is still polling, so a reader that unmounts mid-run and comes back after the run
    // finished would otherwise be served the episodes as of the unmount.
    queryKey: ["run-heal-episodes", workflowRunId, workflowRun?.status],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get<RunHealEpisodesResponse>(`/runs/${workflowRunId}/heal_episodes`)
        .then((response) => response.data);
    },
    // The status swap lands on an uncached key, and both readers render null on absent data — so
    // without this the heal chip and the block heal panel blink out as the run finishes.
    placeholderData: keepPreviousData,
    refetchOnMount:
      workflowRun && statusIsNotFinalized(workflowRun) ? "always" : false,
    refetchOnWindowFocus:
      workflowRun && statusIsNotFinalized(workflowRun) ? "always" : false,
    enabled: Boolean(workflowRunId) && enabled,
  });
}

export { useRunHealEpisodesQuery };
