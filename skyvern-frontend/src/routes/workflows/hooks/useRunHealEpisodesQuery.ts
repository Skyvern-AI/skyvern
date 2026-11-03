import { useEffect, useRef } from "react";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  getRunAttempt,
  runIsExecuting,
} from "@/routes/workflows/workflowRun/runRetryState";
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

  const executing = Boolean(workflowRun && runIsExecuting(workflowRun));
  const attempt = workflowRun ? getRunAttempt(workflowRun) : 1;
  const previousRunUpdateRef = useRef({
    workflowRunId,
    dataUpdatedAt,
    executing,
  });
  useEffect(() => {
    const previous = previousRunUpdateRef.current;
    // Include the stop transition so the final heal episode is fetched, but
    // ignore status polls that only confirm a retry wait.
    if (
      dataUpdatedAt !== previous.dataUpdatedAt &&
      workflowRunId &&
      (executing ||
        (previous.workflowRunId === workflowRunId && previous.executing))
    ) {
      queryClient.invalidateQueries({
        queryKey: ["run-heal-episodes", workflowRunId],
      });
    }
    previousRunUpdateRef.current = {
      workflowRunId,
      dataUpdatedAt,
      executing,
    };
  }, [dataUpdatedAt, workflowRunId, executing, queryClient]);

  return useQuery<RunHealEpisodesResponse>({
    // Same reason the run timeline carries its status: the invalidation above only fires while the
    // run query is still polling, so a reader that unmounts mid-run and comes back after the run
    // finished would otherwise be served the episodes as of the unmount.
    queryKey: [
      "run-heal-episodes",
      workflowRunId,
      workflowRun?.status,
      attempt,
      workflowRun?.retry_pending,
    ],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get<RunHealEpisodesResponse>(`/runs/${workflowRunId}/heal_episodes`)
        .then((response) => response.data);
    },
    // The status swap lands on an uncached key, and both readers render null on absent data — so
    // without this the heal chip and the block heal panel blink out as the run finishes.
    placeholderData: (previousData, previousQuery) =>
      previousQuery &&
      previousQuery.queryKey[1] === workflowRunId &&
      previousQuery.queryKey[3] === attempt
        ? keepPreviousData(previousData)
        : undefined,
    refetchOnMount: executing ? "always" : false,
    refetchOnWindowFocus: executing ? "always" : false,
    enabled: Boolean(workflowRunId) && enabled,
  });
}

export { useRunHealEpisodesQuery };
