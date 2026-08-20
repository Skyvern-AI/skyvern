import { useEffect, useRef } from "react";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import { useQuery, useQueryClient } from "@tanstack/react-query";
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
    queryKey: ["run-heal-episodes", workflowRunId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get<RunHealEpisodesResponse>(`/runs/${workflowRunId}/heal_episodes`)
        .then((response) => response.data);
    },
    refetchOnMount:
      workflowRun && statusIsNotFinalized(workflowRun) ? "always" : false,
    refetchOnWindowFocus:
      workflowRun && statusIsNotFinalized(workflowRun) ? "always" : false,
    enabled: Boolean(workflowRunId) && enabled,
  });
}

export { useRunHealEpisodesQuery };
